"""Tests for the workflow guide and the get_workflow_guide tool (Issue #728)."""
from __future__ import annotations

import re

import pytest

from sunaba import server, workflow_guide

# -- Helpers -----------------------------------------------------------------


def _guide_text() -> str:
    """Read the workflow guide markdown directly from the source tree."""
    import pathlib
    return (pathlib.Path(workflow_guide.__file__).resolve().parent / "workflow_guide.md").read_text("utf-8")


def _backtick_identifiers(text: str) -> set[str]:
    """Extract lowercase snake_case identifiers from inline backticks.

    Also includes underscored identifiers from indented code blocks so that
    tool names used only in examples (e.g. ``sandbox_initialize``) are caught.
    """
    idents: set[str] = set()
    # Inline backticks: all identifiers
    for m in re.findall(r"`([^`]+)`", text):
        for ident in re.findall(r"\b[a-z][a-z0-9_]*\b", m):
            if len(ident) >= 2:
                idents.add(ident)
    # Indented / fenced code blocks: only underscored identifiers (avoids
    # common English words like ``the``, ``from``, ``foo``).
    in_fence = False
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if line.startswith("    ") or in_fence:
            for ident in re.findall(r"\b[a-z][a-z0-9_]*\b", stripped):
                if len(ident) >= 2 and "_" in ident:
                    idents.add(ident)
    return idents


def _tools():
    """Return the list of registered tool metadata."""
    import asyncio
    return asyncio.run(server.mcp.list_tools())


# -- Tests -------------------------------------------------------------------


class TestGetWorkflowGuide:
    """The ``get_workflow_guide`` tool returns the guide correctly."""

    def test_full_guide(self) -> None:
        """Returns the whole guide when no phase is given."""
        result = workflow_guide.get_guide()
        assert result.startswith("# sunaba workflow guide")
        assert "this guide wins" in result
        assert "## phase: init" in result
        assert "## phase: publish" in result

    def test_guide_contains_winning_paragraph(self) -> None:
        """The guide states it wins over client-side documents."""
        result = workflow_guide.get_guide()
        assert "If this guide contradicts" in result
        assert "this guide wins" in result

    def test_phase_filtered_output_includes_preamble(self) -> None:
        """Phase-filtered output must still carry the "this guide wins"
        provision so a caller reading only one phase sees it."""
        for phase_name in ("init", "edit", "verify", "publish", "issue",
                           "explore", "cleanup"):
            section = workflow_guide.get_guide(phase_name)
            assert "this guide wins" in section, (
                f"phase {phase_name!r} output is missing the preamble"
            )

    def test_phase_filter_init(self) -> None:
        """Filtering by phase returns only that section."""
        section = workflow_guide.get_guide("init")
        assert "## phase: init" in section
        assert "sandbox_initialize" in section
        assert "## phase: publish" not in section

    def test_phase_filter_publish(self) -> None:
        section = workflow_guide.get_guide("publish")
        assert "## phase: publish" in section
        assert "secret_scan_override" in section

    def test_phase_filter_explore(self) -> None:
        section = workflow_guide.get_guide("explore")
        assert "## phase: explore" in section
        assert "search_in_container" in section

    def test_phase_filter_edit(self) -> None:
        section = workflow_guide.get_guide("edit")
        assert "## phase: edit" in section

    def test_phase_filter_verify(self) -> None:
        section = workflow_guide.get_guide("verify")
        assert "## phase: verify" in section

    def test_phase_filter_issue(self) -> None:
        section = workflow_guide.get_guide("issue")
        assert "## phase: issue" in section

    def test_phase_filter_cleanup(self) -> None:
        section = workflow_guide.get_guide("cleanup")
        assert "## phase: cleanup" in section

    def test_unknown_phase_returns_error(self) -> None:
        """An unrecognized phase returns an error that names valid phases."""
        result = workflow_guide.get_guide("nonexistent_phase")
        assert result.startswith("Error: unknown phase")
        assert "init" in result
        assert "publish" in result
        assert "edit" in result
        assert "verify" in result

    def test_unknown_phase_does_not_raise(self) -> None:
        """An unrecognized phase returns a string, never raises."""
        result = workflow_guide.get_guide("bogus")
        assert isinstance(result, str)


class TestDriftDetection:
    """Every backtick-quoted snake_case identifier in the guide must be a
    registered tool name, a parameter name of some registered tool, or a
    member of the explicit allowlist below.

    Renaming a tool or parameter without updating the guide fails this test.
    """

    # -- Explicit allowlist for non-sunaba terms ---------------------------
    # Each entry is commented with its justification.  Keep this list as
    # small as possible.

    ALLOWLIST: set[str] = {
        # External CLI tools (not sunaba tools)
        "docker",       # Docker CLI (e.g. ``docker ps``)
        "gh",           # GitHub CLI (e.g. ``gh pr view``)
        "git",          # git CLI (e.g. ``git diff --stat``)

        # git concepts and subcommands
        "origin",       # git remote name (``origin/<branch>``)
        "diff",         # git subcommand (``git diff --stat``)
        "stat",         # git diff flag (``git diff --stat``)
        "check",        # git concept / general term
        "default",      # "default branch" concept

        # External tools / frameworks
        "pytest",       # Python test framework, not a sunaba tool

        # JSON / return-value field names that are not tool parameters
        "additions",              # diff_in_container per-file summary field
        "deletions",              # diff_in_container per-file summary field
        "diff_summary",           # verify_in_container return JSON key
        "gate_passed",            # verify_in_container return JSON field
        "lint_type_incomplete",   # verify_in_container return JSON field
        "merge_discarded_sha",    # publish return JSON field
        "merge_discarded_undeclared",  # publish return JSON field
        "mergeable",              # gh pr view JSON field
        "parents",                # gh pr view JSON field
        "staged",                 # diff_summary JSON key
        "staged_files",           # publish return JSON field
        "status",                 # common JSON return field (many tools)
        "unstaged",               # diff_summary JSON key
        "untracked",              # diff_summary JSON key
        "worktree_leftover",      # publish return JSON field

        # Values, examples, and non-identifier terms
        "comment",      # value for ``method`` parameter of sandbox_issue_write
        "foo",          # example placeholder value
        "json",         # data format / gh CLI flag value
        "no",           # from ``--no-verify`` flag
        "old_string",   # explicitly documented as the WRONG argument name
        "out",          # CLI output reference in code examples
        "owner",        # example in "owner/repo" pattern
        "ps",           # from ``docker ps``
        "py",           # file extension ".py"
        "tests",        # directory name in ``path="tests/"`` argument
        "verify",       # from ``--no-verify``; also a common verb prefix
        "view",         # from ``gh pr view`` subcommand

        # English words used in code-block examples
        "from",
        "the",

        # Environment variable / configuration constants
        "SUNABA_ALLOWED_REPOS",          # egress-proxy allowlist constant
        "SUNABA_CONTAINER_TTL_SECONDS",  # container TTL configuration constant
    }

    def test_no_drift(self) -> None:
        text = _guide_text()
        extracted = _backtick_identifiers(text)

        tools = _tools()
        tool_names = {t.name for t in tools}
        param_names: set[str] = set()
        for t in tools:
            props = (t.parameters or {}).get("properties") or {}
            param_names.update(props.keys())

        unknown = extracted - tool_names - param_names - self.ALLOWLIST
        assert not unknown, (
            "The following backtick-quoted identifiers in workflow_guide.md "
            "are not registered tool names, parameter names, or allowlisted. "
            "Either a tool/parameter was renamed without updating the guide, "
            "or a new entry needs to be added to the ALLOWLIST set above "
            f"(with a comment justifying it):\n  {sorted(unknown)}"
        )


class TestLoadGuideRobustness:
    """_load_guide falls back only for a genuinely absent resource.

    Anything else means the packaging is broken and must propagate with its
    real cause intact -- swallowing it would surface later as a bare
    FileNotFoundError from the source-tree fallback, which in an installed
    deployment cannot succeed either.
    """

    def test_falls_back_when_resource_is_absent(self, monkeypatch) -> None:
        """A missing package resource falls back to the source tree."""
        import importlib.resources as ilr

        monkeypatch.setattr(ilr, "files", lambda _pkg: _raise_resource_absent())
        text = workflow_guide._load_guide()  # noqa: SLF001
        assert "this guide wins" in text, "fallback should have loaded the guide"

    def test_unexpected_failure_propagates(self, monkeypatch) -> None:
        """A broken-packaging error is not swallowed by the fallback."""
        import importlib.resources as ilr

        monkeypatch.setattr(ilr, "files", lambda _pkg: _raise_packaging_broken())
        with pytest.raises(OSError, match="simulated unreadable package resource"):
            workflow_guide._load_guide()  # noqa: SLF001


def _raise_resource_absent() -> None:
    """Simulate the resource genuinely not being there.

    This is the only class of failure _load_guide is allowed to swallow,
    because the source-tree fallback can actually recover from it.
    """
    raise FileNotFoundError("simulated missing package resource")


def _raise_packaging_broken() -> None:
    """Simulate a failure that means the packaging itself is broken.

    A permission error, a corrupt wheel, an unreadable zip.  The
    source-tree fallback cannot recover from any of these in an installed
    deployment, so swallowing them would only replace a diagnosable error
    with a misleading one.
    """
    raise OSError("simulated unreadable package resource")


class TestAdvisoryInReturnValues:
    """sandbox_initialize and sandbox_attach carry an advisory naming
    get_workflow_guide.  run_container_and_exec does NOT.

    These tests verify the advisory at the source-code level because the
    sandbox environment does not expose a Docker socket.
    """

    LIFECYCLE_PATH = "src/sunaba/tools/container/lifecycle.py"

    def test_sandbox_initialize_has_advisory(self) -> None:
        import pathlib
        src = (pathlib.Path(server.__file__).resolve().parent
               / "tools" / "container" / "lifecycle.py").read_text("utf-8")
        # The advisory string must appear in the function body, near the
        # return statement of sandbox_initialize.
        assert "get_workflow_guide" in src
        assert "if you intend to edit or publish" in src

    def test_sandbox_attach_has_advisory(self) -> None:
        """sandbox_attach's *returned JSON* carries the advisory.

        Asserted at runtime rather than by searching lifecycle.py, so an
        early return or an advisory set after json.dumps is caught.
        sandbox_initialize is already covered this way by the exact-match
        assertions in test_container_lifecycle / test_exec_tools /
        test_issue181_resilience / test_issue298_init_leak / test_pr_branch;
        sandbox_attach had no runtime assertion of its own.
        """
        import json as _json
        from unittest.mock import MagicMock, patch

        from sunaba.security import MANAGED_LABEL, NAME_LABEL
        from sunaba.tools.container import sandbox_attach

        container = MagicMock()
        container.id = "abc123def456"
        container.status = "running"
        container.image.tags = ["python:3.12"]
        container.image.short_id = "sha256:xyz"
        container.labels = {MANAGED_LABEL: "true", NAME_LABEL: "my-container"}
        container.exec_run.side_effect = [(0, (b"main\n", b"")), (0, (b"", b""))]

        client = MagicMock()
        client.containers.list.return_value = [container]

        with (
            patch("sunaba.tools.container._docker", return_value=client),
            patch(
                "sunaba.tools.container.lifecycle.resolve_git_root",
                return_value="/home/sandbox",
            ),
            patch(
                "sunaba.tools.container.lifecycle.read_journal",
                return_value=[],
            ),
            patch(
                "sunaba.tools.container.lifecycle.checkpoint_list",
                return_value=_json.dumps({"checkpoints": []}),
            ),
        ):
            result = _json.loads(sandbox_attach("my-container"))

        assert result["found"] is True
        assert "get_workflow_guide" in result["advisory"]
        # The condition must be in the wording: a caller who attached only
        # to read has to be able to skip it by its own judgment.
        assert "intend" in result["advisory"]

    def test_run_container_and_exec_no_advisory(self) -> None:
        import pathlib
        src = (pathlib.Path(server.__file__).resolve().parent
               / "tools" / "container" / "lifecycle.py").read_text("utf-8")
        # The advisory string must NOT appear anywhere in
        # run_container_and_exec's code path (after the function def).
        # Find the def and check the body.
        idx = src.find("def run_container_and_exec(")
        assert idx != -1
        body = src[idx:]
        # The advisory should not appear in this function body.
        assert "get_workflow_guide" not in body


# -- Phase enumeration test -------------------------------------------------


class TestPhaseParsing:
    """Phase names are discovered from the markdown, not hard-coded."""

    def test_all_phases_are_discovered(self) -> None:
        """Every ## phase: header in the guide is a valid phase."""
        text = _guide_text()
        phases = workflow_guide._parse_phases(text)  # noqa: SLF001
        assert "init" in phases
        assert "explore" in phases
        assert "edit" in phases
        assert "verify" in phases
        assert "publish" in phases
        assert "issue" in phases
        assert "cleanup" in phases

    def test_each_phase_returns_content(self) -> None:
        """Each discovered phase returns non-empty content."""
        phases = workflow_guide._parse_phases(_guide_text())
        for phase in phases:
            section = workflow_guide.get_guide(phase)
            assert section, f"phase {phase!r} returned empty content"
