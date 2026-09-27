"""Wire-level tests for opt-in compact results."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import mcp.types as mt
import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.tools import ToolResult
from jsonschema import validate

from sunaba.result_format import ResultFormatMiddleware, compact_result


def _roundtrip(query: str):
    async def run():
        mcp = FastMCP("result-test")
        calls = []

        @mcp.tool()
        def sample() -> str:
            calls.append(1)
            return json.dumps({"status": "ok", "output": "payload\n" * 100})

        mcp.add_middleware(ResultFormatMiddleware())
        app = mcp.http_app()
        result = None
        error = None
        async with app.router.lifespan_context(app):
            def factory(headers=None, timeout=None, auth=None, **kwargs: Any):
                kwargs.pop("transport", None)
                return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         headers=headers, timeout=timeout, auth=auth, **kwargs)
            transport = StreamableHttpTransport(
                url="http://127.0.0.1/mcp/" + query, httpx_client_factory=factory,
            )
            try:
                async with Client(transport) as client:
                    tools = await client.list_tools()
                    response = await client.call_tool("sample", {})
                    result = (tools[0].output_schema, response, calls)
            except Exception as exc:
                error = exc
        if error:
            raise error
        return result
    return asyncio.run(run())


@pytest.mark.parametrize("query", ["", "?response=legacy"])
def test_legacy_wire_unchanged(query):
    schema, response, calls = _roundtrip(query)
    assert calls == [1]
    assert json.loads(response.structured_content["result"])["status"] == "ok"
    assert json.loads(response.content[0].text)["status"] == "ok"
    validate(response.structured_content, schema)


def test_compact_wire_contains_payload_once_and_matches_schema():
    schema, response, calls = _roundtrip("?response=compact")
    assert calls == [1]
    assert response.structured_content == {"status": "ok", "output": "payload\n" * 100}
    assert "payload" not in response.content[0].text
    validate(response.structured_content, schema)
    # A later legacy connection must not inherit the compact schema.
    old_schema, legacy, _ = _roundtrip("")
    assert "result" in old_schema["properties"]
    validate(legacy.structured_content, old_schema)


def test_invalid_mode_rejected():
    with pytest.raises(Exception, match="Unknown response format"):
        _roundtrip("?response=compcat")


@pytest.mark.parametrize(("raw", "expected"), [
    ('{"status":"error","error":"details"}', {"status": "error", "error": "details"}),
    ("[1,2]", {"items": [1, 2]}),
    ("Error: cannot connect\ntry again", {"result": "Error: cannot connect\ntry again"}),
    ("null", {"result": None}),
])
def test_payload_and_error_metadata_preserved(raw, expected):
    original = ToolResult(content=raw, structured_content={"result": raw},
                          is_error=True, meta={"trace": "id"})
    converted = compact_result(original)
    assert converted.structured_content == expected
    assert converted.is_error is True
    assert converted.meta == {"trace": "id"}


def test_rich_or_native_results_untouched():
    native = ToolResult(content="text", structured_content={"answer": 42})
    assert compact_result(native) is native
    rich = ToolResult(content=[
        mt.TextContent(type="text", text="text"),
        mt.ImageContent(type="image", data="AA==", mimeType="image/png"),
    ], structured_content={"result": "text"})
    assert compact_result(rich) is rich
