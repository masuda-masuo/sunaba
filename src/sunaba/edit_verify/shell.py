"""Shell-command building blocks: path quoting and environment prefixes.

These are used by runners in :mod:`sunaba.edit_verify` to construct
shell commands that execute inside sandbox containers.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from typing import Any


def _quote_path(path: str | Sequence[str]) -> str:
    """Shell-escape one or more file paths for use in a command string."""
    if isinstance(path, str):
        return shlex.quote(path)
    return " ".join(shlex.quote(p) for p in path)


def _path_display(path: str | Sequence[str]) -> str:
    """Render *path* as a single string for use as a parse-fallback label."""
    return path if isinstance(path, str) else " ".join(path)


#: Environment variables to set before running linters/type checkers
#: inside sandbox containers.  Containers run as a non-root user with
#: a read-only ``/``, so cache directories must point to ``/tmp``.
_SANDBOX_ENV: str = (
    "RUFF_CACHE_DIR=/tmp/.ruff_cache "
    "mkdir -p /tmp/.ruff_cache 2>/dev/null; "
)


def _exec_run(
    container: Any,
    cmd: list[str],
    workdir: str | None = None,
) -> tuple[int, str, str]:
    """Run ``container.exec_run`` with ``demux=True``.

    docker-py *only* returns a ``(stdout, stderr)`` tuple when
    ``demux=True`` is passed; without it the output is multiplexed
    bytes with no way to separate the streams.  This wrapper
    centralises the call so every caller gets clean decoded text
    without repeating the idiom.

    Returns:
        ``(exit_code, stdout_text, stderr_text)``.
    """
    exit_code, (stdout_part, stderr_part) = container.exec_run(
        cmd,
        stdout=True,
        stderr=True,
        demux=True,
        workdir=workdir,
    )
    # docker-py hands back ``None`` -- not ``b""`` -- for a stream that
    # produced nothing, so both parts are guarded before decoding.
    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""
    return exit_code, stdout_text, stderr_text


#: Environment prefix for *go* invocations only (Issue #584).
#:
#: ``GOMAXPROCS=1`` serialises the Go toolchain's compile/vet/link fan-out,
#: which otherwise blows past the container's ``pids_limit`` of 100 and dies of
#: fork exhaustion (#233).  It used to be baked into ``Dockerfile.go`` as an
#: image-wide ``ENV`` -- but *every* Go binary honours ``GOMAXPROCS``, and ``gh``
#: is written in Go, so the image-wide setting throttled unrelated tools.  Once
#: the go toolchain lives in the all-in-one default image (``sandbox:full``)
#: that leak reaches every container, so the guard moves to where it belongs:
#: the go command itself.
_GO_ENV: str = "GOMAXPROCS=1 "
