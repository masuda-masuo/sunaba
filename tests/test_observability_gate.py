"""Tests for the opt-in journal/trace read-tool gate (#460).

Telemetry writes are unconditional; the five read tools
(``sandbox_read_journal`` / ``sandbox_trace`` / ``sandbox_list_runs`` /
``sandbox_journal_path`` / ``sandbox_trace_dir``) are only registered
when ``CODE_SANDBOX_OBSERVABILITY_TOOLS`` is set to a truthy value at import time.
"""
from __future__ import annotations

import asyncio
import importlib
import os

import pytest

from code_sandbox_mcp import server
from code_sandbox_mcp.server import OBSERVABILITY_TOOLS_ENV

READ_TOOLS = {
    "sandbox_read_journal",
    "sandbox_trace",
    "sandbox_list_runs",
    "sandbox_journal_path",
    "sandbox_trace_dir",
}


class TestObservabilityToolsEnabled:
    """Unit tests for the flag parser."""

    def test_unset_is_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(OBSERVABILITY_TOOLS_ENV, raising=False)
        assert server.observability_tools_enabled() is False

    def test_empty_is_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(OBSERVABILITY_TOOLS_ENV, "")
        assert server.observability_tools_enabled() is False

    def test_zero_is_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(OBSERVABILITY_TOOLS_ENV, "0")
        assert server.observability_tools_enabled() is False

    def test_one_is_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(OBSERVABILITY_TOOLS_ENV, "1")
        assert server.observability_tools_enabled() is True


@pytest.fixture
def reload_server():
    """Reload the server module under a controlled env value.

    Registration happens at import time, so the gate can only be
    exercised by re-importing the module.  Teardown restores the
    original environment and reloads once more so later tests see the
    module in its original state.
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


def _tool_names(mod) -> set[str]:
    return {t.name for t in asyncio.run(mod.mcp.list_tools())}


class TestRegistrationGate:
    """The five read tools appear on the tool list only when opted in."""

    def test_disabled_by_default(self, reload_server) -> None:
        mod = reload_server(None)
        names = _tool_names(mod)
        assert names.isdisjoint(READ_TOOLS)
        # Core tools are unaffected by the gate
        assert "sandbox_exec" in names
        assert "publish" in names

    def test_enabled_registers_read_tools(self, reload_server) -> None:
        mod = reload_server("1")
        names = _tool_names(mod)
        assert READ_TOOLS <= names
