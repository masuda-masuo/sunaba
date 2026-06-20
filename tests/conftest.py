"""Shared fixtures for all tests.

An autouse fixture patches ``get_cached_result`` and ``set_cached_result``
so existing (and new) tests are never accidentally affected by real cache data
written to ``~/.code-sandbox-mcp/cache/`` by a previous test run.

Tests that need to verify cache behaviour can still override these mocks
by patching the same targets with custom return values (decorators or
context managers).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _mock_result_cache() -> None:
    """Prevent all tests from reading/writing real cache data.

    Without this fixture, a test that calls ``sandbox_exec`` (directly or
    indirectly) may silently read stale cache entries written by a prior
    test, causing order-dependent failures.
    """
    with (
        patch("code_sandbox_mcp.server.get_cached_result", return_value=None),
        patch("code_sandbox_mcp.server.set_cached_result"),
    ):
        yield
