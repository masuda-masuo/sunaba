"""Package install tools: package_install."""

from __future__ import annotations

import json
import shlex
from typing import Annotated

from docker.errors import NotFound
from pydantic import BeforeValidator

from sunaba.journal import record_exec as journal_record_exec
from sunaba.tools.common import _coerce_list_arg, _docker


def _run_in_container(container_id: str, cmd: list[str]) -> tuple[int, str, str]:
    """Run a shell command inside the container and return (exit_code, stdout, stderr)."""
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return -1, "", f"Container {container_id[:12]} not found"
    except Exception as e:
        return -1, "", str(e)

    exit_code, output = container.exec_run(
        cmd,
        stdout=True,
        stderr=True,
        demux=True,
    )
    stdout_part, stderr_part = output
    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""
    return exit_code, stdout_text, stderr_text


def _get_installed_packages(container_id: str) -> list[dict[str, str]]:
    """Get the current list of installed packages via ``pip list --format=json``."""
    ec, stdout, stderr = _run_in_container(container_id, ["pip", "list", "--format=json"])
    if ec != 0:
        return []
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return []


def _package_to_key(pkg: dict[str, str]) -> str:
    return f"{pkg['name']}=={pkg.get('version', '?')}"


def package_install(
    container_id: str,
    packages: Annotated[str | list[str], BeforeValidator(_coerce_list_arg)] | None = None,
    editable: str | None = None,
    constraints: str | None = None,
    requirements: str | None = None,
    upgrade: bool = False,
    extras: str | None = None,
) -> str:
    """Install Python packages inside the sandbox container.

    A first-class tool for ``pip install`` that returns structured output
    (installed packages, changed count, error details) instead of raw pip
    logs.  This is the recommended way to install Python packages inside
    the container.

    Args:
        container_id: 12-character container ID prefix.
        packages: Package name(s) to install.  Can be a single string
            (e.g. ``"requests"``) or a list of strings
            (e.g. ``["requests", "click"]``).  Accepts any format that
            ``pip install`` accepts: package names, VCS URLs, local paths,
            or ``package[extra]`` syntax.
        editable: Path to a local project for editable install
            (``pip install -e <path>``).  Mutually exclusive with *packages*.
        constraints: Path to a constraints file inside the container
            (``pip install -c <file>``).
        requirements: Path to a requirements file inside the container
            (``pip install -r <file>``).
        upgrade: When ``True``, pass ``--upgrade`` to pip (default ``False``).
        extras: Extras string for editable install
            (e.g. ``"[dev]"`` → ``pip install -e ".[dev]"``).
            Only meaningful when *editable* is set.

    Returns:
        JSON string with fields:

        * ``status``: ``"ok"`` on success, ``"error"`` on failure.
        * ``installed_packages``: list of ``"name==version"`` strings
          that were newly installed or changed.
        * ``changed``: number of packages installed/changed.
        * ``output``: short human-readable output from pip.
        * ``error``: error description on failure.
        * ``stderr``: raw stderr on failure.
    """
    # --- Validate arguments ---
    if not any([packages, editable, constraints, requirements]):
        return json.dumps({
            "status": "error",
            "error": "One of packages, editable, constraints, or requirements is required",
        })

    if packages and editable:
        return json.dumps({
            "status": "error",
            "error": "packages and editable are mutually exclusive",
        })

    # --- Build install arguments (shared by both installers) ---
    install_args: list[str] = ["install"]

    if upgrade:
        install_args.append("--upgrade")

    if constraints:
        install_args.extend(["-c", constraints])

    if requirements:
        install_args.extend(["-r", requirements])

    if editable:
        install_args.extend(["-e", editable])
        if extras:
            install_args[-1] = f"{editable}{extras}"
    elif packages:
        if isinstance(packages, str):
            install_args.append(packages)
        else:
            install_args.extend(packages)

    # --- Choose the installer at runtime inside the container (#390) ---
    # Images with the persistent sandbox-owned venv (PR #388) set
    # ``$VIRTUAL_ENV``; there ``uv pip install`` works and is much faster.
    # Venv-less images (older pins, custom images) fall back to plain
    # ``pip``, whose user-site (``~/.local``) fallback is the only working
    # path for a non-root user: uv has no ``--user`` and ``--system`` hits
    # root-owned site-packages (#380 / #383).
    quoted_args = " ".join(shlex.quote(a) for a in install_args)
    install_cmd = [
        "sh",
        "-c",
        'if [ -n "$VIRTUAL_ENV" ] && command -v uv >/dev/null 2>&1; '
        f"then exec uv pip {quoted_args}; "
        f"else exec pip {quoted_args}; fi",
    ]

    # --- Snapshot installed packages before ---
    before = _get_installed_packages(container_id)
    before_keys = {_package_to_key(p) for p in before}

    # --- Run the install ---
    ec, stdout_text, stderr_text = _run_in_container(container_id, install_cmd)

    # Record the install in the audit journal.  package_install mutates
    # container state (and may reach the network), so it must leave a trail
    # just like ``sandbox_exec pip install ...`` does; a dedicated tool must
    # not become an audit blind spot (Issue #359).
    journal_record_exec(
        container_id[:12],
        install_cmd,
        ec,
        verbose="package_install",
    )

    # --- Snapshot installed packages after ---
    after = _get_installed_packages(container_id)
    after_keys = {_package_to_key(p) for p in after}

    new_or_changed = sorted(after_keys - before_keys)

    if ec != 0:
        return json.dumps({
            "status": "error",
            "error": f"package install failed (exit code {ec})",
            "stderr": stderr_text or stdout_text,
            "installed_packages": new_or_changed,
            "changed": len(new_or_changed),
        })

    return json.dumps({
        "status": "ok",
        "installed_packages": new_or_changed,
        "changed": len(new_or_changed),
        "output": stdout_text.strip() or (stderr_text.strip() if stderr_text else ""),
    })
