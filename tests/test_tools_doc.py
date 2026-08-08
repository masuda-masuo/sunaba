"""Keep docs/tools.md honest against the real tool signatures (Issue #573).

The reference table drifted from the code once already: it documented
``write_file_sandbox(path, content, mode)`` when the tool takes
``file_name``/``file_contents``, a ``pr`` parameter on ``clone_repo`` that
never existed, and it omitted ``publish(create_pr=...)`` -- the flag that
decides whether a PR is opened at all.  A doc that lies about the interface
misroutes both humans and agents, so the table is verified mechanically:
every registered tool is documented, every documented parameter exists
(nothing invented), no parameter — required or optional — is missing,
and the `(opt)` marker always matches optionality.
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


def _registered_tools() -> dict[str, tuple[str, frozenset[str]]]:
    """Map exposed tool name -> (function name, excluded args).

    Reads ``server.py`` statically rather than importing it, so the check
    covers the opt-in observability tools (registered only when
    ``SUNABA_OBSERVABILITY_TOOLS`` is set) exactly like the default ones.
    Registration-time ``exclude_args=[...]`` hides parameters from the
    client interface (the ``_container`` parameter on the merge tools), so
    they are exempt from the documentation requirement alongside the
    injected FastMCP ``ctx``.
    """
    tree = ast.parse(SERVER_PY.read_text())
    tools: dict[str, tuple[str, frozenset[str]]] = {}
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
        excluded: set[str] = set()
        for kw in inner.keywords:
            if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                exposed = str(kw.value.value)
            elif kw.arg == "exclude_args" and isinstance(kw.value, ast.List):
                for elt in kw.value.elts:
                    if isinstance(elt, ast.Constant):
                        excluded.add(str(elt.value))
        tools[exposed] = (func_name, frozenset(excluded))
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


def _documented() -> dict[str, tuple[set[str], set[str]]]:
    """Map tool name -> (parameter names named in its row, marked (opt)).

    The ``(opt)`` marker is part of the table's contract: the preamble
    promises "Required parameters are listed first; `(opt)` marks optional
    ones", so a row that mislabels requiredness (marks a required parameter
    ``(opt)`` or omits the marker on an optional one) must fail CI too.
    """
    documented: dict[str, tuple[set[str], set[str]]] = {}
    for line in TOOLS_MD.read_text().splitlines():
        row = re.match(r"\|\s*`(\w+)`\s*\|([^|]*)\|", line)
        if not row:
            continue
        name, params = row.group(1), row.group(2)
        named = set(re.findall(r"`(\w+)`", params))
        marked_opt = set(re.findall(r"`(\w+)`\s*\(opt", params))
        documented[name] = (named, marked_opt)
    return documented


def test_every_registered_tool_is_documented() -> None:
    missing = sorted(set(_registered_tools()) - set(_documented()))
    assert not missing, f"tools missing from docs/tools.md: {missing}"


def test_no_documented_tool_is_unregistered() -> None:
    extra = sorted(set(_documented()) - set(_registered_tools()))
    assert not extra, f"docs/tools.md documents tools that do not exist: {extra}"


@pytest.mark.parametrize("tool_name", sorted(_registered_tools()))
def test_documented_parameters_match_signature(tool_name: str) -> None:
    func_name, excluded = _registered_tools()[tool_name]
    func = _tool_function(func_name)
    params = inspect.signature(func).parameters
    real = set(params)
    required = {
        name
        for name, p in params.items()
        if p.default is inspect.Parameter.empty
        and p.kind
        not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    }
    documented, marked_opt = _documented()[tool_name]

    invented = sorted(documented - real)
    assert not invented, (
        f"docs/tools.md lists parameters {tool_name} does not accept: {invented}"
    )

    undocumented_required = sorted(required - documented)
    assert not undocumented_required, (
        f"docs/tools.md omits required parameters of {tool_name}: "
        f"{undocumented_required}"
    )

    # No parameter may go undocumented, optional ones included.  Exempt only
    # what the client never sees: registration-time exclude_args (the
    # _container parameter on the merge tools) and FastMCP's injected ctx
    # (sandbox_initialize_tool).
    client_visible = real - set(excluded)
    if func_name == "sandbox_initialize_tool":
        client_visible -= {"ctx"}
    undocumented = sorted(client_visible - documented)
    assert not undocumented, (
        f"docs/tools.md omits parameters of {tool_name}: {undocumented}"
    )

    # The preamble promises "Required parameters are listed first; (opt)
    # marks optional ones" -- enforce the marker, not just presence: every
    # optional parameter must carry it, and no required parameter may.
    optional = client_visible - required
    missing_opt_marker = sorted(optional - marked_opt)
    assert not missing_opt_marker, (
        f"docs/tools.md omits the (opt) marker on optional parameters of "
        f"{tool_name}: {missing_opt_marker}"
    )
    required_with_marker = sorted(required & marked_opt)
    assert not required_with_marker, (
        f"docs/tools.md marks required parameters of {tool_name} as (opt): "
        f"{required_with_marker}"
    )
