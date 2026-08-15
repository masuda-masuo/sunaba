"""Phase-based tool profiles: server-side ``tools/list`` filtering (Issue #782).

The always-loaded cost of an MCP session is dominated by the tool list itself.
Every worker session pays for every tool definition, including the ones its
phase never calls: an implement worker carries the issue tools, a review
worker carries the write tools.  Client-side deny lists do not help -- they
guard the call, after the definition has already been loaded.

A client configured with ``http://127.0.0.1:8750/mcp?profile=implement`` gets
only that phase's tools; a client with no ``profile`` parameter gets the full
list exactly as before.  MCP clients POST every request to their configured
URL, so the parameter rides along on the ``tools/list`` POST.

This is a **context-size measure, not a security control**.  Calling a tool
the profile did not list still works: capability guards stay where they are
(the client-side allowlists and the server's own checks).  Filtering the list
only keeps the definitions out of the session.

Profile definitions live here, server-side, for the same reason the workflow
guide does (#728): they ship in the same wheel as the server, so they always
describe the server actually running.  ``tool_profile`` is used throughout in
preference to a bare ``profile`` because ``SecurityProfile`` (container
hardening, ``security.py``) already owns that word; only the public query
parameter is called ``profile``.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol, TypeVar

import mcp.types as mt
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import Tool
from mcp.shared.exceptions import McpError
from mcp.types import INVALID_PARAMS, ErrorData

from .workflow_guide import _load_guide, _parse_phases

# The query parameter is public surface (it appears in every client's MCP
# configuration), so it keeps the short name from the issue.
PROFILE_QUERY_PARAM = "profile"

# Tools every profile carries regardless of the phases it covers: the guide is
# how a worker recovers the contract for the tools it *does* have.
ALWAYS_INCLUDED = frozenset({"get_workflow_guide"})


# ---------------------------------------------------------------------------
# Workflow-guide alignment
# ---------------------------------------------------------------------------

_BACKTICK_SPAN = re.compile(r"`([^`]+)`")
_LEADING_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")


def guide_phase_tools(known_tools: Collection[str]) -> dict[str, frozenset[str]]:
    """Map each ``## phase:`` section of the workflow guide to the tools it names.

    A tool is "named" when a backtick span in the section starts with its
    name, so both ``search_in_container`` and ``checkpoint(container_id,
    message)`` count.  Spans that do not resolve to a known tool (``old_str``,
    ``worktree=True``, ``gate_passed``) are ignored -- the guide stays free to
    use backticks for arguments and response fields.

    Args:
        known_tools: The registered tool surface to match spans against.
            Passing it in keeps this function pure and lets the caller decide
            which surface counts (the observability gate changes it).
    """
    text = _load_guide()
    lines = text.split("\n")
    known = set(known_tools)
    result: dict[str, frozenset[str]] = {}
    for phase, (start, end) in _parse_phases(text).items():
        section = "\n".join(lines[start:end])
        named: set[str] = set()
        for span in _BACKTICK_SPAN.findall(section):
            m = _LEADING_IDENTIFIER.match(span)
            if m and m.group(0) in known:
                named.add(m.group(0))
        result[phase] = frozenset(named)
    return result


# ---------------------------------------------------------------------------
# Profile definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolProfile:
    """One named tool subset, served for ``?profile=<name>``.

    Attributes:
        name: The value of the ``profile`` query parameter.
        phases: The workflow-guide phases this profile claims to cover.  The
            alignment test (#782 acceptance 3) requires every tool named in
            those guide sections to be in ``tools`` or in ``excluded``.
        tools: The tool names served for this profile.  Declared data, not
            derived from the guide, so guide drift shows up as a test failure
            instead of a silent change of surface.
        beyond_guide: ``tool name -> reason`` for members the covered guide
            sections do not name (the guide documents them in prose, in a
            code block, or under another name).
        excluded: ``tool name -> reason`` for deliberate omissions.  A tool
            named in a covered guide section may only be missing from
            ``tools`` if it is listed here.
    """

    name: str
    phases: tuple[str, ...]
    tools: frozenset[str]
    beyond_guide: Mapping[str, str]
    excluded: Mapping[str, str]


_IMPLEMENT = ToolProfile(
    name="implement",
    phases=("explore", "edit", "verify", "publish"),
    tools=frozenset(
        {
            "get_workflow_guide",
            # explore
            "search_in_container",
            "read_file_range",
            "list_files",
            "run_python",
            "sandbox_exec",
            "sandbox_exec_background",
            "sandbox_exec_check",
            # edit
            "write_file",
            "edit_file",
            "transform_file",
            "undo_file_edit",
            "checkpoint",
            "checkpoint_list",
            "checkpoint_restore",
            # verify
            "verify_in_container",
            "lint_in_container",
            "type_check_in_container",
            "package_install",
            "diff_in_container",
            # publish
            "publish",
            "secret_scan_override",
        }
    ),
    beyond_guide={
        "sandbox_exec_background": "async half of the exec family; the guide documents it under sandbox_exec",
        "sandbox_exec_check": "reads back a sandbox_exec_background job",
        "lint_in_container": "the verify section calls it 'the lint gate' in prose, not by tool name",
        "type_check_in_container": "the verify section calls it 'the type gate' in prose, not by tool name",
        "package_install": "installing a missing dependency is part of the edit/verify loop",
        "diff_in_container": "documented under a prose subheading in the verify section, not backticked",
        "publish": "the publish section shows the call in a code block, which carries no backticks",
    },
    excluded={
        # Container lifecycle is the orchestrator's, by kusabi convention: a
        # worker attaches to the container named in its brief.
        "sandbox_initialize": "orchestrator-only: the worker is handed a container",
        "sandbox_attach": "orchestrator-only: the worker is handed a container id",
        "sandbox_stop": "orchestrator-only: teardown belongs to the cleanup phase",
        "sandbox_list_containers": "orchestrator-only: cleanup-phase discovery",
        "run_container_and_exec": "one-shot lifecycle wrapper; same reason as sandbox_initialize",
        # Host->container transfer: a worker's inputs arrive with the container.
        "copy_file": "host-side input path; not part of the edit loop",
        "copy_project": "host-side input path; not part of the edit loop",
        # Integration work is decided outside the implement phase.
        "merge_base": "integration is orchestrator work",
        "merge_complete": "integration is orchestrator work",
        "merge_abort": "integration is orchestrator work",
        # Issue/PR surfaces have their own profiles.
        "issue_view": "issue surface: see the issue profile",
        "sandbox_issue_write": "issue surface: see the issue profile",
        "sandbox_pr_review_write": "review surface: see the review profile",
    },
)

_REVIEW = ToolProfile(
    name="review",
    phases=("explore", "verify"),
    tools=frozenset(
        {
            "get_workflow_guide",
            # explore (read/search only -- the exec tools are excluded below)
            "search_in_container",
            "read_file_range",
            "list_files",
            # verify
            "verify_in_container",
            "lint_in_container",
            "type_check_in_container",
            "diff_in_container",
            # the reviewer's only write channel
            "sandbox_pr_review_write",
        }
    ),
    beyond_guide={
        "lint_in_container": "the verify section calls it 'the lint gate' in prose, not by tool name",
        "type_check_in_container": "the verify section calls it 'the type gate' in prose, not by tool name",
        "diff_in_container": "documented under a prose subheading in the verify section, not backticked",
        "sandbox_pr_review_write": "the review verdict itself; named in the guide's issue phase",
    },
    excluded={
        # Named in a covered guide phase (explore) and deliberately dropped:
        # both run caller-supplied code inside the container, which a review
        # session has no reason to do.
        "sandbox_exec": "runs caller-supplied commands: not read-only",
        "run_python": "runs caller-supplied code: not read-only",
        "sandbox_exec_background": "same as sandbox_exec",
        "sandbox_exec_check": "reads back a job this profile cannot start",
        # The write surface #782 acceptance 7 pins as absent.
        "write_file": "review does not modify the tree",
        "edit_file": "review does not modify the tree",
        "transform_file": "review does not modify the tree",
        "undo_file_edit": "review does not modify the tree",
        "copy_file": "review does not modify the tree",
        "copy_project": "review does not modify the tree",
        "checkpoint": "savepoints belong to the phase that makes the edits",
        "checkpoint_restore": "rollback belongs to the phase that makes the edits",
        "checkpoint_list": "savepoint bookkeeping belongs to the implement phase",
        "publish": "review never pushes",
        "package_install": "review never mutates the environment",
        "secret_scan_override": "a publish-gate approval; review does not publish",
    },
)

_ISSUE = ToolProfile(
    name="issue",
    phases=("issue",),
    tools=frozenset(
        {
            "get_workflow_guide",
            "issue_view",
            "read_file_range",
            "sandbox_issue_write",
            "sandbox_pr_review_write",
        }
    ),
    beyond_guide={
        "issue_view": "the guide's issue section documents the write tools; reading is the same surface",
        "read_file_range": (
            "issue_view saves the full issue body to a file inside the container; "
            "reading that file back is the documented follow-up, and the guide's "
            "issue section does not name the read tool"
        ),
    },
    # Nothing is excluded here, and the profile is *not* container-free.
    # Every registered sunaba tool but get_workflow_guide and the recovery
    # pair (sandbox_stop, sandbox_list_containers) is registered
    # docker_bound -- issue_view included: it takes a mandatory container_id
    # and saves the full issue body into that container.  So this profile is
    # the minimal issue surface, not a containerless one; a session on
    # ?profile=issue still works against the container named in its brief,
    # exactly like every other profile.
    excluded={},
)

TOOL_PROFILES: Mapping[str, ToolProfile] = MappingProxyType(
    {p.name: p for p in (_IMPLEMENT, _REVIEW, _ISSUE)}
)


# ---------------------------------------------------------------------------
# Resolution and filtering
# ---------------------------------------------------------------------------


def valid_tool_profile_names() -> tuple[str, ...]:
    """Return the defined profile names, sorted."""
    return tuple(sorted(TOOL_PROFILES))


def lookup_tool_profile(name: str) -> ToolProfile:
    """Return the profile called *name*.

    An unknown name is a loud protocol error, never a silent full list: a
    typo in a client configuration must surface as a broken session rather
    than as the context saving quietly not happening.

    Args:
        name: The ``profile`` query-parameter value.

    Raises:
        McpError: If *name* is not a defined profile.
    """
    profile = TOOL_PROFILES.get(name)
    if profile is None:
        valid = ", ".join(valid_tool_profile_names())
        raise McpError(
            ErrorData(
                code=INVALID_PARAMS,
                message=(
                    f"unknown tool profile {name!r}; valid profiles: {valid} "
                    f"(omit ?{PROFILE_QUERY_PARAM}= for the full tool list)"
                ),
            )
        )
    return profile


def resolve_tool_profile() -> ToolProfile | None:
    """Return the profile selected by the current HTTP request, if any.

    This is the single decision point for "which profile applies here".
    ``None`` means "do not filter": either there is no HTTP request in
    context (in-memory transport, embedded use) or the request carries no
    ``profile`` parameter.  A present-but-empty value is a configuration
    slip, so it goes through the unknown-profile error instead.

    Raises:
        McpError: If the request names a profile that does not exist.
    """
    try:
        request = get_http_request()
    except RuntimeError:
        return None
    if PROFILE_QUERY_PARAM not in request.query_params:
        return None
    return lookup_tool_profile(request.query_params[PROFILE_QUERY_PARAM])


class _NamedTool(Protocol):
    """Structural type of the objects ``filter_tools`` selects from."""

    name: str


_T = TypeVar("_T", bound=_NamedTool)


def filter_tools(tools: Sequence[_T], tool_profile: str | ToolProfile) -> list[_T]:
    """Return the members of *tools* that *tool_profile* lists.

    Purely subtractive: a profile can never add a tool the server did not
    register, so the observability gate (#460) keeps the last word on its
    five read tools.

    Args:
        tools: The unfiltered tool sequence.
        tool_profile: A profile name or a resolved profile.

    Raises:
        McpError: If a profile name is given and does not exist.
    """
    profile = (
        tool_profile
        if isinstance(tool_profile, ToolProfile)
        else lookup_tool_profile(tool_profile)
    )
    return [tool for tool in tools if tool.name in profile.tools]


class ToolProfileMiddleware(Middleware):
    """Filter ``tools/list`` down to the profile named by the HTTP request."""

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        """Return the full list, or the selected profile's subset of it."""
        # Resolved before call_next so an unknown profile fails without
        # building a list nobody will read.
        profile = resolve_tool_profile()
        tools = await call_next(context)
        if profile is None:
            return tools
        return filter_tools(tools, profile)
