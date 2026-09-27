"""Opt-in compact MCP responses; legacy clients keep their wire contract."""
from __future__ import annotations

import json
from collections.abc import Sequence

import mcp.types as mt
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import Tool, ToolResult
from mcp.shared.exceptions import MCPError


def compact_requested() -> bool:
    """Resolve per-request format before any tool side effect."""
    try:
        request = get_http_request()
    except RuntimeError:
        return False
    value = request.query_params.get("response", "legacy")
    if value not in ("legacy", "compact"):
        raise MCPError(
            code=mt.INVALID_PARAMS,
            message="Unknown response format; use response=legacy or response=compact",
        )
    return value == "compact"


def compact_result(result: ToolResult) -> ToolResult:
    """Unwrap only FastMCP's string result envelope; preserve other results."""
    structured = result.structured_content
    if not (isinstance(structured, dict) and set(structured) == {"result"}
            and isinstance(structured["result"], str)):
        return result
    # Do not discard images, resources, or additional content blocks.
    if len(result.content) != 1 or not isinstance(result.content[0], mt.TextContent):
        return result
    raw = structured["result"]
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        value = raw
    if isinstance(value, dict):
        payload = value
    elif isinstance(value, list):
        payload = {"items": value}
    else:
        payload = {"result": value}
    # Text-only clients should use legacy mode. The payload occurs once on
    # the compact wire, in structuredContent; this is just a short summary.
    summary = {key: payload[key] for key in (
        "status", "gate_passed", "busy", "shown", "total_lines", "truncated",
        "next_offset", "has_more", "output_id", "output_unavailable",
    ) if key in payload}
    label = json.dumps(summary, ensure_ascii=False) if summary else "See structuredContent."
    return ToolResult(
        content=[mt.TextContent(type="text", text="Result: " + label[:500])],
        structured_content=payload, meta=result.meta, is_error=result.is_error,
    )


class ResultFormatMiddleware(Middleware):
    """Keep tools/list schemas consistent with the selected response format."""

    async def on_list_tools(
        self, context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        """Advertise the request-specific structured output schema."""
        compact = compact_requested()
        tools = await call_next(context)
        if not compact:
            return tools
        # Copies are request-local: never change another client's schema.
        return [tool.model_copy(update={
            "output_schema": {"type": "object", "additionalProperties": True},
        }) for tool in tools]

    async def on_call_tool(
        self, context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Convert results only after validating the requested format."""
        compact = compact_requested()
        result = await call_next(context)
        return compact_result(result) if compact else result
