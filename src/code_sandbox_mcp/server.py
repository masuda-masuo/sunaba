"""MCP server for Docker sandbox execution with pass-through-env support.

Inspired by Automata-Labs-team/code-sandbox-mcp.
"""
from __future__ import annotations

import argparse
import os
import io
import tarfile
import tempfile
from pathlib import Path

import docker
from docker.errors import APIError, NotFound
from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Argument parsing (runs at import time so FastMCP can start cleanly)
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="code-sandbox-mcp: Docker sandbox MCP server"
    )
    parser.add_argument(
        "--pass-through-env",
        metavar="VAR1,VAR2,...",
        default="",
        help="Comma-separated list of environment variable names to pass into containers",
    )
    # fastmcp may add its own flags; use parse_known_args to avoid conflicts
    args, _ = parser.parse_known_args()
    return args


_args = _parse_args()

# Build the set of env var names to pass through
_PASS_THROUGH_KEYS: list[str] = [
    k.strip() for k in _args.pass_through_env.split(",") if k.strip()
]


def _container_env() -> dict[str, str]:
    """Return env vars that should be injected into every new container."""
    return {
        key: os.environ[key]
        for key in _PASS_THROUGH_KEYS
        if key in os.environ
    }


# ---------------------------------------------------------------------------
# Docker client helper
# ---------------------------------------------------------------------------

def _docker() -> docker.DockerClient:
    return docker.from_env()


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("code-sandbox-mcp")


@mcp.tool()
def sandbox_initialize(image: str = "python:3.12-slim-bookworm") -> str:
    """Initialize a new compute environment for code execution.

    Creates a Docker container based on the specified image.
    Returns a container_id that must be passed to other sandbox tools.

    Args:
        image: Docker image to use (default: python:3.12-slim-bookworm)
    """
    client = _docker()
    env = _container_env()
    container = client.containers.run(
        image,
        command="sleep infinity",
        detach=True,
        remove=False,
        environment=env,
    )
    return container.id


@mcp.tool()
def sandbox_exec(container_id: str, commands: list[str]) -> str:
    """Execute commands sequentially inside a running container.

    Runs each command via 'sh -c'. Stops on first non-zero exit code.
    Returns combined stdout/stderr output with exit codes.

    Args:
        container_id: ID returned by sandbox_initialize
        commands: List of shell commands to run in order
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"

    output_parts: list[str] = []
    for cmd in commands:
        output_parts.append(f"$ {cmd}")
        exit_code, output = container.exec_run(
            ["sh", "-c", cmd],
            stdout=True,
            stderr=True,
            demux=False,
        )
        decoded = output.decode("utf-8", errors="replace") if output else ""
        if decoded:
            output_parts.append(decoded.rstrip("\n"))
        if exit_code != 0:
            output_parts.append(f"Command exited with code {exit_code}")
            break

    return "\n".join(output_parts)


@mcp.tool()
def sandbox_stop(container_id: str) -> str:
    """Stop and remove a running container sandbox.

    Args:
        container_id: ID returned by sandbox_initialize
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
        container.stop(timeout=10)
        container.remove(v=True)
        return f"Container {container_id[:12]} stopped and removed"
    except NotFound:
        return f"Container {container_id[:12]} not found (already removed?)"
    except APIError as e:
        return f"Error: {e}"


@mcp.tool()
def write_file_sandbox(
    container_id: str,
    file_name: str,
    file_contents: str,
    dest_dir: str = "/root",
) -> str:
    """Write a file into the container filesystem.

    Args:
        container_id: ID returned by sandbox_initialize
        file_name: Name of the file to create
        file_contents: Text content to write
        dest_dir: Directory inside the container (default: /root)
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"

    encoded = file_contents.encode("utf-8")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=file_name)
        info.size = len(encoded)
        tar.addfile(info, io.BytesIO(encoded))
    buf.seek(0)

    try:
        container.put_archive(dest_dir, buf)
        return f"Written {file_name} to {dest_dir} in container {container_id[:12]}"
    except APIError as e:
        return f"Error: {e}"


@mcp.tool()
def copy_project(
    container_id: str,
    local_src_dir: str,
    dest_dir: str = "/root",
) -> str:
    """Copy a local directory into the container filesystem.

    Args:
        container_id: ID returned by sandbox_initialize
        local_src_dir: Absolute path to a directory on the host
        dest_dir: Destination directory inside the container (default: /root)
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"

    src = Path(local_src_dir)
    if not src.is_dir():
        return f"Error: {local_src_dir} is not a directory"

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.add(str(src), arcname=src.name)
    buf.seek(0)

    try:
        container.put_archive(dest_dir, buf)
        return f"Copied {local_src_dir} to {dest_dir}/{src.name} in container {container_id[:12]}"
    except APIError as e:
        return f"Error: {e}"


@mcp.tool()
def copy_file(
    container_id: str,
    local_src_file: str,
    dest_path: str = "/root",
) -> str:
    """Copy a single local file into the container filesystem.

    Args:
        container_id: ID returned by sandbox_initialize
        local_src_file: Absolute path to a file on the host
        dest_path: Destination directory inside the container (default: /root)
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"

    src = Path(local_src_file)
    if not src.is_file():
        return f"Error: {local_src_file} is not a file"

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.add(str(src), arcname=src.name)
    buf.seek(0)

    try:
        container.put_archive(dest_path, buf)
        return f"Copied {src.name} to {dest_path} in container {container_id[:12]}"
    except APIError as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
