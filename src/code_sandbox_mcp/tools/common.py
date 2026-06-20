"""Shared helpers for code_sandbox_mcp tools."""

from __future__ import annotations

from typing import Any


def _docker() -> Any:
    """Lazy-import docker and return a Docker client."""
    import docker

    return docker.from_env()
