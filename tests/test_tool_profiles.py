"""Phase-based tool profiles filter ``tools/list`` server-side (Issue #782).

The always-loaded cost of a worker session is the tool list itself, so a
client may select a phase profile with ``.../mcp?profile=implement``.  These
tests pin the three properties that make that safe: the profiles do not drift
from the workflow guide, an unknown profile fails loudly, and a request
without the parameter still sees the complete surface.
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import os
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from mcp.shared.exceptions import McpError

from sunaba import server
from sunaba.server import OBSERVABILITY_TOOLS_ENV
from sunaba.tool_profiles import (
    ALWAYS_INCLUDED,
    PROFILE_QUERY_PARAM,
    TOOL_PROFILES,
    ToolProfile,
    filter_tools,
    guide_phase_tools,
    lookup_tool_profile,
    resolve_tool_profile,
)

# The registered surface as of #782.  Profiles are subtractive, so no member
# of this set may disappear from an unfiltered tools/list; later work may add
# tools, which is why this is a subset check and not equality.
SURFACE_AT_782 = frozenset(
    {
        "checkpoint",
        "checkpoint_list",
        "checkpoint_restore",
        "copy_file",
        "copy_project",
        "diff_in_container",
        "edit_file",
        "get_workflow_guide",
        "issue_view",
        "lint_in_container",
        "list_files",
        "merge_abort",
        "merge_base",
        "merge_complete",
        "package_install",
        "publish",
        "read_file_range",
        "run_container_and_exec",
        "run_python",
        "sandbox_attach",
        "sandbox_exec",
        "sandbox_exec_background",
        "sandbox_exec_check",
        "sandbox_initialize",
        "sandbox_issue_write",
        "sandbox_list_containers",
        "sandbox_pr_review_write",
        "sandbox_stop",
        "search_in_container",
        "secret_scan_override",
        "transform_file",
        "type_check_in_container",
        "undo_file_edit",
        "verify_in_container",
        "write_file",
    }
)

# Registered only when SUNABA_OBSERVABILITY_TOOLS is set (#460); mirrors the
# list in tests/test_observability_gate.py.
OBSERVABILITY_TOOLS = frozenset(
    {
        "sandbox_read_journal",
        "sandbox_trace",
        "sandbox_list_runs",
        "sandbox_journal_path",
        "sandbox_trace_dir",
    }
)

# Everything an issue-phase session is meant to carry, named explicitly: the
# GitHub issue tools, the guide every profile gets, and read_file_range --
# issue_view saves the full issue body to a file inside the container, so
# reading that file back is the documented follow-up.  The profile is the
# *minimal* issue surface; it is not container-free, and the classification
# below is read from the server's registrations rather than restated here so
# that a claim like "no container-bound tools" cannot survive in this file.
ISSUE_PROFILE_SURFACE = frozenset(
    {
        "get_workflow_guide",
        "issue_view",
        "read_file_range",
        "sandbox_issue_write",
        "sandbox_pr_review_write",
    }
)

SERVER_PY = Path(__file__).resolve().parent.parent / "src" / "sunaba" / "server.py"

# Registration-time decorators that say how a tool reaches docker (#784).
_BINDINGS = {"docker_bound": "docker", "recovery_bound": "recovery"}


def _tool_bindings() -> dict[str, str]:
    """Map exposed tool name -> "docker", "recovery" or "unbound".

    Parsed from ``src/sunaba/server.py`` -- the same registration source
    ``tests/test_tools_doc.py`` reads -- so the classification is the
    server's, not a list maintained in this file.  ``mcp.tool()`` is applied
    on top of ``docker_bound`` / ``recovery_bound``, both rebinding the same
    module-level name, so the decorator applied to a function name carries
    over to the tool it is registered as (``name=`` overrides the exposed
    name, as for sandbox_initialize).  Static parsing also covers the
    observability tools, which register only under the #460 env gate.
    """
    tree = ast.parse(SERVER_PY.read_text())
    bound: dict[str, str] = {}
    exposed: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        # Shape: <func> = docker_bound(<func>, ...) / recovery_bound(<func>)
        if isinstance(call.func, ast.Name) and call.func.id in _BINDINGS:
            if call.args and isinstance(call.args[0], ast.Name):
                bound[call.args[0].id] = _BINDINGS[call.func.id]
            continue
        # Shape: <name> = mcp.tool(...)(<func>)
        inner = call.func
        if not (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == "tool"
            and isinstance(inner.func.value, ast.Name)
            and inner.func.value.id == "mcp"
        ):
            continue
        if not (call.args and isinstance(call.args[0], ast.Name)):
            continue
        func_name = call.args[0].id
        name = func_name
        for kw in inner.keywords:
            if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                name = str(kw.value.value)
        exposed[name] = func_name
    return {name: bound.get(func, "unbound") for name, func in exposed.items()}


# Tools that neither modify container state nor run caller-supplied code.
# The verify gates are here because they run the repository's own checks --
# no caller code, no writes -- which is why #782 keeps them in the review
# profile.  sandbox_attach is here because attaching binds the session to
# the container id the brief hands it without creating or modifying a
# container (#860).  Anything not listed counts as write/exec for the review
# test, so a newly added tool has to be classified deliberately.
READ_ONLY_TOOLS = frozenset(
    {
        "get_workflow_guide",
        "read_file_range",
        "list_files",
        "search_in_container",
        "diff_in_container",
        "checkpoint_list",
        "lint_in_container",
        "type_check_in_container",
        "verify_in_container",
        "issue_view",
        "sandbox_attach",
        "sandbox_list_containers",
    }
)

# The one deliberate exception to the read-only classification (#860): the
# kusabi review-phase allowlist (REVIEW_ALLOWED_TOOLS in
# plugins/kusabi/scripts/claude-dispatch.mjs) grants sandbox_exec to
# reviewers, so the review profile carries it even though it runs
# caller-supplied commands.  Read-only-ness is a client-side capability
# statement; profiles are a context-size measure, not a security control
# (module docstring of tool_profiles.py) -- the client allowlist keeps the
# capability.
GRANTED_EXEC_TOOLS = frozenset({"sandbox_exec"})

PROFILE_NAMES = sorted(TOOL_PROFILES)


def _registered() -> set[str]:
    """Tool names on the unfiltered surface (no HTTP request -> no filtering)."""
    return {t.name for t in asyncio.run(server.mcp.list_tools())}


def _guide_tools(profile: ToolProfile, known: set[str]) -> set[str]:
    """Tools named by the guide sections *profile* claims to cover."""
    per_phase = guide_phase_tools(known)
    named: set[str] = set()
    for phase in profile.phases:
        named |= per_phase[phase]
    return named


class _QueryRecorder:
    """ASGI wrapper recording the query string of every HTTP request."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.requests: list[tuple[str, str]] = []

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            self.requests.append(
                (scope["method"], scope["query_string"].decode("latin-1"))
            )
        await self.app(scope, receive, send)


def _http_tool_names(
    query: str = "",
    wrap: Callable[[Any], Any] | None = None,
) -> list[str]:
    """List tools over a real streamable-http round trip against mcp.http_app().

    The MCP client POSTs to the configured URL, so *query* rides along on
    every request of the session -- including tools/list.
    """

    async def _run() -> list[str]:
        app = server.mcp.http_app()
        asgi = wrap(app) if wrap is not None else app
        # A failure must not cross the lifespan boundary: the session
        # manager's task group would rewrap it in an ExceptionGroup and hide
        # the server's message.  Capture it here, re-raise outside.
        names: list[str] = []
        failure: Exception | None = None
        async with app.router.lifespan_context(app):

            # Named parameters match fastmcp's McpHttpClientFactory protocol;
            # **kwargs takes the extras it also passes (follow_redirects).
            def factory(
                headers: dict[str, str] | None = None,
                timeout: httpx.Timeout | None = None,
                auth: httpx.Auth | None = None,
                **kwargs: Any,
            ) -> httpx.AsyncClient:
                kwargs.pop("transport", None)
                return httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=asgi),
                    headers=headers,
                    timeout=timeout,
                    auth=auth,
                    **kwargs,
                )

            transport = StreamableHttpTransport(
                url=f"http://127.0.0.1/mcp/{query}", httpx_client_factory=factory
            )
            try:
                async with Client(transport) as client:
                    names = [t.name for t in await client.list_tools()]
            except Exception as exc:
                failure = exc
        if failure is not None:
            raise failure
        return names

    return asyncio.run(_run())


class TestProfileDefinitions:
    """Structural invariants of the declared profiles."""

    @pytest.mark.parametrize("name", PROFILE_NAMES)
    def test_includes_the_workflow_guide(self, name: str) -> None:
        assert ALWAYS_INCLUDED <= TOOL_PROFILES[name].tools

    @pytest.mark.parametrize("name", PROFILE_NAMES)
    def test_members_are_registered_tools(self, name: str) -> None:
        unknown = sorted(TOOL_PROFILES[name].tools - _registered())
        assert not unknown, f"{name} lists tools the server does not register: {unknown}"

    @pytest.mark.parametrize("name", PROFILE_NAMES)
    def test_declared_phases_exist_in_the_guide(self, name: str) -> None:
        phases = set(guide_phase_tools(_registered()))
        missing = sorted(set(TOOL_PROFILES[name].phases) - phases)
        assert not missing, f"{name} claims phases the guide does not define: {missing}"

    @pytest.mark.parametrize("name", PROFILE_NAMES)
    def test_exclusions_are_real_tools_with_reasons(self, name: str) -> None:
        profile = TOOL_PROFILES[name]
        unknown = sorted(set(profile.excluded) - _registered())
        assert not unknown, f"{name} excludes tools that do not exist: {unknown}"
        contradictory = sorted(set(profile.excluded) & profile.tools)
        assert not contradictory, (
            f"{name} both lists and excludes: {contradictory}"
        )
        assert all(reason.strip() for reason in profile.excluded.values())

    @pytest.mark.parametrize("name", PROFILE_NAMES)
    def test_beyond_guide_entries_have_reasons(self, name: str) -> None:
        assert all(
            reason.strip() for reason in TOOL_PROFILES[name].beyond_guide.values()
        )

    def test_every_registered_tool_has_a_binding(self) -> None:
        # The parsed registrations must cover the live surface, or the
        # classification the issue tests rely on would silently miss a tool.
        unclassified = sorted(_registered() - set(_tool_bindings()))
        assert not unclassified, (
            f"server.py registrations not recognised by _tool_bindings: {unclassified}"
        )

    @pytest.mark.parametrize("name", PROFILE_NAMES)
    def test_no_profile_lists_an_observability_tool(self, name: str) -> None:
        # The env gate (#460) keeps the last word: a profile may never name a
        # tool the gate excluded.
        assert not TOOL_PROFILES[name].tools & OBSERVABILITY_TOOLS


class TestGuideAlignment:
    """Profiles stay aligned with the workflow guide (#782 acceptance 3).

    Both sources are read at runtime: the phase sections of
    ``src/sunaba/workflow_guide.md`` and the declared profile membership.
    Adding a tool to a guide phase without updating the profile that covers
    it fails the first test; adding a tool to a profile that no covered guide
    section names fails the second unless the reason is declared.
    """

    @pytest.mark.parametrize("name", PROFILE_NAMES)
    def test_guide_named_tools_are_covered_or_explicitly_excluded(
        self, name: str
    ) -> None:
        profile = TOOL_PROFILES[name]
        known = _registered()
        missing = sorted(
            _guide_tools(profile, known) - profile.tools - set(profile.excluded)
        )
        assert not missing, (
            f"guide phases {list(profile.phases)} name {missing}, which the "
            f"{name} profile neither lists nor excludes -- add them to tools, "
            f"or to excluded with a reason"
        )

    @pytest.mark.parametrize("name", PROFILE_NAMES)
    def test_members_outside_the_covered_phases_are_declared(self, name: str) -> None:
        profile = TOOL_PROFILES[name]
        known = _registered()
        outside = profile.tools - _guide_tools(profile, known) - ALWAYS_INCLUDED
        assert outside == set(profile.beyond_guide), (
            f"{name}: members outside the covered guide phases are {sorted(outside)}, "
            f"declared beyond_guide is {sorted(profile.beyond_guide)}"
        )


class TestProfileContents:
    """The membership rules #782 states as acceptance criteria."""

    def test_implement_carries_the_edit_verify_publish_tooling(self) -> None:
        tools = TOOL_PROFILES["implement"].tools
        assert {
            "write_file",
            "edit_file",
            "transform_file",
            "undo_file_edit",
            "sandbox_exec",
            "search_in_container",
            "read_file_range",
            "checkpoint",
            "checkpoint_restore",
            "verify_in_container",
            "lint_in_container",
            "type_check_in_container",
            "package_install",
            "run_python",
            "diff_in_container",
            "publish",
            "sandbox_attach",
            "issue_view",
        } <= tools

    def test_review_has_no_write_tools_beyond_the_granted_exec(self) -> None:
        tools = TOOL_PROFILES["review"].tools
        forbidden = {
            "write_file",
            "edit_file",
            "transform_file",
            "undo_file_edit",
            "copy_file",
            "copy_project",
            "checkpoint_restore",
            "publish",
            "package_install",
            "sandbox_exec_background",
            "run_python",
        }
        assert not tools & forbidden
        # sandbox_pr_review_write is the reviewer's one write channel, and it
        # writes to GitHub, not to the container.  sandbox_exec is the one
        # exec exception: the kusabi review allowlist grants it (#860).
        assert "sandbox_pr_review_write" in tools
        assert tools <= (
            READ_ONLY_TOOLS | GRANTED_EXEC_TOOLS | {"sandbox_pr_review_write"}
        )

    def test_profiles_are_supersets_of_the_kusabi_phase_allowlists(self) -> None:
        # Consumer contract: the kusabi phase allowlists
        # (plugins/kusabi/scripts/claude-dispatch.mjs, IMPLEMENT_ALLOWED_TOOLS
        # / REVIEW_ALLOWED_TOOLS) grant implement workers sandbox_attach +
        # issue_view and review workers sandbox_attach + issue_view +
        # sandbox_exec.  Each profile must stay a superset of its consumer's
        # allowlist so an accidental removal fails here instead of surfacing
        # as a mystery missing tool in a worker session (kusabi#860).
        assert {"sandbox_attach", "issue_view"} <= TOOL_PROFILES["implement"].tools
        assert (
            {"sandbox_attach", "issue_view", "sandbox_exec"}
            <= TOOL_PROFILES["review"].tools
        )

    def test_issue_is_exactly_the_minimal_issue_surface(self) -> None:
        tools = TOOL_PROFILES["issue"].tools
        assert tools == ISSUE_PROFILE_SURFACE
        assert {"issue_view", "sandbox_issue_write"} <= tools

    def test_issue_is_contained_in_the_issue_surface(self) -> None:
        # Containment by name, and the classification of what is left out
        # comes from the server's own registrations: every docker-bound tool
        # outside the issue surface is off the profile, and so is the
        # recovery pair (sandbox_stop / sandbox_list_containers), which is
        # cleanup-phase work.
        tools = TOOL_PROFILES["issue"].tools
        bindings = _tool_bindings()
        assert tools <= ISSUE_PROFILE_SURFACE
        container_bound = {n for n, b in bindings.items() if b == "docker"}
        assert not tools & (container_bound - ISSUE_PROFILE_SURFACE)
        assert not tools & {n for n, b in bindings.items() if b == "recovery"}

    def test_issue_profile_still_needs_a_container(self) -> None:
        # The contract this profile actually has: issue_view is registered
        # docker_bound (it takes a mandatory container_id and saves the issue
        # body into the container), so is read_file_range, and so is every
        # other member but the guide.  A profile "with no container-bound
        # tools" is therefore not something the issue profile can be, and a
        # future edit that assumes otherwise fails here.
        bindings = _tool_bindings()
        assert bindings["issue_view"] == "docker"
        assert bindings["read_file_range"] == "docker"
        tools = TOOL_PROFILES["issue"].tools
        needs_container = {n for n in tools if bindings[n] == "docker"}
        assert {"issue_view", "read_file_range"} <= needs_container
        assert needs_container == tools - ALWAYS_INCLUDED
        assert all(bindings[n] == "unbound" for n in ALWAYS_INCLUDED)


class TestFilterTools:
    """The pure filter and the profile lookup."""

    def test_filter_selects_exactly_the_profile(self) -> None:
        tools = asyncio.run(server.mcp.list_tools())
        for name, profile in TOOL_PROFILES.items():
            selected = {t.name for t in filter_tools(tools, name)}
            assert selected == set(profile.tools)

    def test_filter_never_adds(self) -> None:
        tools = [t for t in asyncio.run(server.mcp.list_tools()) if t.name != "publish"]
        assert "publish" not in {t.name for t in filter_tools(tools, "implement")}

    def test_unknown_profile_names_the_valid_ones(self) -> None:
        with pytest.raises(McpError) as excinfo:
            lookup_tool_profile("implementt")
        message = str(excinfo.value)
        assert "implementt" in message
        for name in PROFILE_NAMES:
            assert name in message

    def test_resolve_without_an_http_request_is_none(self) -> None:
        assert resolve_tool_profile() is None


class TestHttpSelection:
    """End-to-end over streamable HTTP, with the query parameter on the POST."""

    def test_no_parameter_returns_the_full_surface(self) -> None:
        names = set(_http_tool_names())
        assert names == _registered()
        assert SURFACE_AT_782 <= names

    @pytest.mark.parametrize("name", PROFILE_NAMES)
    def test_profile_returns_exactly_its_subset(self, name: str) -> None:
        names = set(_http_tool_names(f"?{PROFILE_QUERY_PARAM}={name}"))
        assert names == set(TOOL_PROFILES[name].tools)
        assert names < _registered()

    def test_the_parameter_rides_on_every_post(self) -> None:
        recorder: dict[str, _QueryRecorder] = {}

        def wrap(app: Any) -> Any:
            recorder["r"] = _QueryRecorder(app)
            return recorder["r"]

        names = set(_http_tool_names(f"?{PROFILE_QUERY_PARAM}=issue", wrap=wrap))
        assert names == set(TOOL_PROFILES["issue"].tools)
        posts = [q for method, q in recorder["r"].requests if method == "POST"]
        assert posts, "no POST reached the app"
        assert all(q == f"{PROFILE_QUERY_PARAM}=issue" for q in posts), posts

    def test_unknown_profile_errors_instead_of_returning_everything(self) -> None:
        with pytest.raises(McpError) as excinfo:
            _http_tool_names(f"?{PROFILE_QUERY_PARAM}=implememt")
        message = str(excinfo.value)
        assert "implememt" in message
        for name in PROFILE_NAMES:
            assert name in message

    def test_empty_parameter_value_is_an_error(self) -> None:
        # A blank value is an unexpanded template in a client config, not a
        # request for the full list.
        with pytest.raises(McpError):
            _http_tool_names(f"?{PROFILE_QUERY_PARAM}=")


class TestInMemoryTransport:
    """The in-memory client has no HTTP request, so it is never filtered."""

    def test_in_memory_client_sees_the_full_surface(self) -> None:
        async def _run() -> set[str]:
            async with Client(server.mcp) as client:
                return {t.name for t in await client.list_tools()}

        names = asyncio.run(_run())
        assert names == _registered()
        assert SURFACE_AT_782 <= names


@pytest.fixture
def reload_server():
    """Reload the server module under a controlled observability-gate value.

    Same pattern as tests/test_observability_gate.py: registration happens at
    import time, so the gate can only be exercised by re-importing.
    """
    original = os.environ.get(OBSERVABILITY_TOOLS_ENV)

    def _reload(value: str | None):
        if value is None:
            os.environ.pop(OBSERVABILITY_TOOLS_ENV, None)
        else:
            os.environ[OBSERVABILITY_TOOLS_ENV] = value
        return importlib.reload(server)

    yield _reload

    if original is None:
        os.environ.pop(OBSERVABILITY_TOOLS_ENV, None)
    else:
        os.environ[OBSERVABILITY_TOOLS_ENV] = original
    importlib.reload(server)


class TestObservabilityGateInteraction:
    """Profiles never resurrect a tool the #460 gate excluded."""

    def test_gated_tools_stay_off_every_profile(self, reload_server) -> None:
        mod = reload_server("1")
        tools = asyncio.run(mod.mcp.list_tools())
        assert OBSERVABILITY_TOOLS <= {t.name for t in tools}
        for name, profile in TOOL_PROFILES.items():
            selected = {t.name for t in filter_tools(tools, name)}
            assert selected == set(profile.tools), name
            assert not selected & OBSERVABILITY_TOOLS

    def test_unfiltered_list_still_carries_them(self, reload_server) -> None:
        mod = reload_server("1")
        names = {t.name for t in asyncio.run(mod.mcp.list_tools())}
        assert OBSERVABILITY_TOOLS | SURFACE_AT_782 <= names
