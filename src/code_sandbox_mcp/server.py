"""MCP server for Docker sandbox execution with pass-through-env support.

Inspired by Automata-Labs-team/code-sandbox-mcp.
"""
from __future__ import annotations

import argparse
import inspect
import io
import logging
import os
import sys
import tarfile
import threading
from pathlib import Path

import docker
from docker.errors import APIError, NotFound
from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Monkey-patch: prevent server crash when client times out
#
# When the MCP client (Claude Desktop / TypingMind) times out after 60 s,
# it disconnects and marks the session as completed.  If our long-running
# sandbox_exec finishes *after* that, FastMCP tries to send the response
# and hits ``AssertionError: Request already responded to``, which kills
# the server.
#
# We replace ``RequestResponder.respond`` with a version that silently
# drops responses on already-completed sessions instead of crashing.
# ---------------------------------------------------------------------------

import mcp.shared.session as _mcp_session  # noqa: E402

_original_respond = _mcp_session.RequestResponder.respond


async def _safe_respond(self, response) -> None:
    if getattr(self, "_completed", False):
        # Client already timed out / disconnected – silently ignore.
        return
    try:
        await _original_respond(self, response)
    except AssertionError:
        # Race: completed flag flipped between check and respond call.
        pass


_mcp_session.RequestResponder.respond = _safe_respond  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("code-sandbox-mcp")


# ---------------------------------------------------------------------------
# Pass-through env keys (populated in main() before mcp.run())
# ---------------------------------------------------------------------------

_PASS_THROUGH_KEYS: list[str] = []
_EXEC_TIMEOUT: int = 300  # Default 5 minutes


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
# Cross-version exec_run helper
# ---------------------------------------------------------------------------

_EXEC_RUN_SUPPORTS_TIMEOUT: bool | None = None


def _exec_run(container, cmd: list[str], **kwargs):
    """Call exec_run with timeout if the SDK supports it, else without.

    Older docker-py versions do not accept a 'timeout' keyword argument.
    We detect support lazily via inspect.signature.
    """
    global _EXEC_RUN_SUPPORTS_TIMEOUT
    if _EXEC_RUN_SUPPORTS_TIMEOUT is None:
        try:
            sig = inspect.signature(container.exec_run)
            _EXEC_RUN_SUPPORTS_TIMEOUT = "timeout" in sig.parameters
        except (ValueError, TypeError):
            _EXEC_RUN_SUPPORTS_TIMEOUT = False

    timeout = kwargs.pop("timeout", None)
    if timeout is not None and not _EXEC_RUN_SUPPORTS_TIMEOUT:
        # SDK doesn't support timeout – use threading-based fallback
        result: list[tuple[int, bytes] | Exception] = []

        def _run():
            try:
                ec, out = container.exec_run(cmd, **kwargs)
                result.append((ec, out))
            except Exception as e:
                result.append(e)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            raise TimeoutError(
                f"Command timed out after {timeout} seconds"
            )
        if isinstance(result[0], Exception):
            raise result[0]
        return result[0]

    if timeout is not None and _EXEC_RUN_SUPPORTS_TIMEOUT:
        kwargs["timeout"] = timeout
    return container.exec_run(cmd, **kwargs)


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
    logger.info("Container %s started (image=%s)", container.id[:12], image)
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
    except Exception as e:
        return f"Error: failed to get container {container_id[:12]}: {e}"

    output_parts: list[str] = []
    for cmd in commands:
        output_parts.append(f"$ {cmd}")
        try:
            exit_code, output = _exec_run(
                container,
                ["sh", "-c", cmd],
                stdout=True,
                stderr=True,
                demux=False,
                timeout=_EXEC_TIMEOUT,
            )
            decoded = output.decode("utf-8", errors="replace") if output else ""
            if decoded:
                output_parts.append(decoded.rstrip("\n"))
            if exit_code != 0:
                output_parts.append(f"Command exited with code {exit_code}")
                break
        except TimeoutError as e:
            output_parts.append(f"Error: {e}")
            break
        except Exception as e:
            output_parts.append(f"Error executing command: {e}")
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
        logger.info("Container %s stopped", container_id[:12])
        return f"Container {container_id[:12]} stopped and removed"
    except NotFound:
        return f"Container {container_id[:12]} not found (already removed?)"
    except APIError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: unexpected error while stopping container: {e}"


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
    except Exception as e:
        return f"Error: failed to get container {container_id[:12]}: {e}"

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
    except Exception as e:
        return f"Error: unexpected error while writing file: {e}"


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
    except Exception as e:
        return f"Error: failed to get container {container_id[:12]}: {e}"

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
    except Exception as e:
        return f"Error: unexpected error while copying project: {e}"


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
    except Exception as e:
        return f"Error: failed to get container {container_id[:12]}: {e}"

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
    except Exception as e:
        return f"Error: unexpected error while copying file: {e}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    # Parse our own args before fastmcp sees sys.argv
    parser = argparse.ArgumentParser(
        description="code-sandbox-mcp: Docker sandbox MCP server",
        add_help=True,
    )
    parser.add_argument(
        "--pass-through-env",
        metavar="VAR1,VAR2,...",
        default="",
        help="Comma-separated list of environment variable names to pass into containers",
    )
    parser.add_argument(
        "--exec-timeout",
        type=int,
        default=300,
        help="Timeout for command execution in seconds (default: 300)",
    )
    args, remaining = parser.parse_known_args()

    # Populate pass-through keys
    global _PASS_THROUGH_KEYS, _EXEC_TIMEOUT
    _PASS_THROUGH_KEYS = [k.strip() for k in args.pass_through_env.split(",") if k.strip()]
    _EXEC_TIMEOUT = args.exec_timeout

    # Replace sys.argv with only what fastmcp should see
    sys.argv = [sys.argv[0]] + remaining

    mcp.run()


if __name__ == "__main__":
    main()