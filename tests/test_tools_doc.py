"""Keep docs/tools.md honest against the real tool signatures (Issue #573).

The reference table drifted from the code once already: it documented
``write_file_sandbox(path, content, mode)`` when the tool takes
``file_name``/``file_contents``, a ``pr`` parameter on ``clone_repo`` that
never existed, and it omitted ``publish(create_pr=...)`` -- the flag that
decides whether a PR is opened at all.  A doc that lies about the interface
misroutes both humans and agents, so the table is verified mechanically:
every registered tool is documented, every documented parameter exists, and
every required parameter is named.
"""
from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path
from typing import Callable

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_PY = REPO_ROOT / "src" / "sunaba" / "server.py"
TOOLS_MD = REPO_ROOT / "docs" / "tools.md"


def _registered_tools() -> dict[str, str]:
    """Map exposed tool name -> the function name it is registered from.

    Reads ``server.py`` statically rather than importing it, so the check
    covers the opt-in observability tools (registered only when
    ``SUNABA_OBSERVABILITY_TOOLS`` is set) exactly like the default ones.
    """
    tree = ast.parse(SERVER_PY.read_text())
    tools: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        # Shape: <name> = mcp.tool(...)(<func>)
        if not isinstance(call.func, ast.Call):
            continue
        inner = call.func
        if not (
            isinstance(inner.func, ast.Attribute)
            and inner.func.attr == "tool"
            and isinstance(inner.func.value, ast.Name)
            and inner.func.value.id == "mcp"
        ):
            continue
        func_name = call.args[0].id if isinstance(call.args[0], ast.Name) else None
        if func_name is None:
            continue
        # mcp.tool(name="...") overrides the exposed name (sandbox_initialize).
        exposed = func_name
        for kw in inner.keywords:
            if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                exposed = str(kw.value.value)
        tools[exposed] = func_name
    return tools


def _tool_function(func_name: str) -> Callable[..., object]:
    """Import the undecorated tool function from its defining module."""
    from sunaba import server

    func = getattr(server, func_name, None)
    if func is None:
        pytest.fail(f"{func_name} is registered but not importable from server")
    # Registered names are rebound to the FastMCP tool object; unwrap to the
    # plain function so inspect.signature sees the real parameters.
    return getattr(func, "fn", func)


def _documented() -> dict[str, set[str]]:
    """Map tool name -> parameter names named in its docs/tools.md row."""
    documented: dict[str, set[str]] = {}
    for line in TOOLS_MD.read_text().splitlines():
        row = re.match(r"\|\s*`(\w+)`\s*\|([^|]*)\|", line)
        if not row:
            continue
        name, params = row.group(1), row.group(2)
        documented[name] = set(re.findall(r"`(\w+)`", params))
    return documented


def test_every_registered_tool_is_documented() -> None:
    missing = sorted(set(_registered_tools()) - set(_documented()))
    assert not missing, f"tools missing from docs/tools.md: {missing}"


def test_no_documented_tool_is_unregistered() -> None:
    extra = sorted(set(_documented()) - set(_registered_tools()))
    assert not extra, f"docs/tools.md documents tools that do not exist: {extra}"


@pytest.mark.parametrize("tool_name", sorted(_registered_tools()))
def test_documented_parameters_match_signature(tool_name: str) -> None:
    func = _tool_function(_registered_tools()[tool_name])
    params = inspect.signature(func).parameters
    real = set(params)
    required = {
        name
        for name, p in params.items()
        if p.default is inspect.Parameter.empty
        and p.kind
        not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    }
    documented = _documented()[tool_name]

    invented = sorted(documented - real)
    assert not invented, (
        f"docs/tools.md lists parameters {tool_name} does not accept: {invented}"
    )

    undocumented_required = sorted(required - documented)
    assert not undocumented_required, (
        f"docs/tools.md omits required parameters of {tool_name}: "
        f"{undocumented_required}"
    )
