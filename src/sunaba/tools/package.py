"""Package install tools: package_install."""

from __future__ import annotations

import json
import re
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


_NPM_CHANGED_RE = re.compile(r"\b(?:added|removed|changed)\s+(\d+)\s+package")


def _npm_changed_summary(output: str) -> int:
    """Parse npm's own "added/removed/changed N packages" summary lines."""
    return sum(int(m.group(1)) for m in _NPM_CHANGED_RE.finditer(output))


def _npm_output_tail(stdout_text: str, stderr_text: str) -> str:
    """Raw output tail for the npm result (npm ci output can be large)."""
    raw = (stdout_text.strip() or stderr_text.strip())
    return raw[-2000:] if len(raw) > 2000 else raw


def _run_npm_install(container_id: str, packages: list[str] | None) -> str:
    """npm axis of package_install: ``npm install <pkgs>`` or project deps.

    With *packages*, installs them in the container's project root.  Without
    them, installs the project deps: ``npm ci`` when ``package-lock.json``
    pins the tree, ``npm install`` otherwise.
    """
    if packages:
        cmd = ["npm", "install", *packages]
        command_label = " ".join(cmd)
    else:
        ec, stdout, _ = _run_in_container(
            container_id,
            ["sh", "-c", "[ -f package-lock.json ] && echo locked || echo unlocked"],
        )
        lockfile = ec == 0 and stdout.strip() == "locked"
        cmd = ["npm", "ci"] if lockfile else ["npm", "install"]
        command_label = " ".join(cmd)

    ec, stdout_text, stderr_text = _run_in_container(container_id, cmd)

    journal_record_exec(
        container_id[:12],
        cmd,
        ec,
        verbose="package_install",
    )

    changed = _npm_changed_summary(stdout_text)
    tail = _npm_output_tail(stdout_text, stderr_text)

    if ec != 0:
        return json.dumps({
            "status": "error",
            "manager": "npm",
            "command": command_label,
            "error": f"{command_label} failed (exit code {ec})",
            "stderr": stderr_text or stdout_text,
            "changed": changed,
            "output": tail,
        })
    return json.dumps({
        "status": "ok",
        "manager": "npm",
        "command": command_label,
        "changed": changed,
        "output": tail,
    })


def package_install(
    container_id: str,
    packages: Annotated[str | list[str], BeforeValidator(_coerce_list_arg)] | None = None,
    editable: str | None = None,
    constraints: str | None = None,
    requirements: str | None = None,
    upgrade: bool = False,
    extras: str | None = None,
    manager: str = "pip",
) -> str:
    """Install packages inside the sandbox container.

    First-class pip install returning structured output instead of raw
    pip logs. manager='npm' installs JS project deps (npm ci when a
    lockfile is present).

    Args:
        container_id: Container ID prefix.
        packages: Package name(s), string or list; any pip install
            spec. Mutually exclusive with editable.
        editable: Project path for pip install -e.
        constraints: Constraints file path in the container (-c).
        requirements: Requirements file path in the container (-r).
        upgrade: Pass --upgrade to pip.
        extras: Extras for the editable install (e.g. '[dev]').
        manager: Package manager: 'pip' (default) or 'npm'.

    Returns:
        JSON: status, installed_packages ("name==version"), changed,
        output; error and stderr on failure.  npm results add manager
        and the command run.
    """
    if manager not in ("pip", "npm"):
        return json.dumps({
            "status": "error",
            "error": f"manager must be 'pip' or 'npm', got {manager!r}",
        })

    if manager == "npm":
        # pip-only arguments have no npm meaning; reject rather than ignore.
        pip_only = [
            name
            for name, val in (
                ("editable", editable),
                ("constraints", constraints),
                ("requirements", requirements),
                ("extras", extras),
                ("upgrade", upgrade),
            )
            if val
        ]
        if pip_only:
            return json.dumps({
                "status": "error",
                "error": f"manager='npm' does not support: {', '.join(pip_only)}",
            })
        pkg_list = None
        if packages:
            pkg_list = [packages] if isinstance(packages, str) else list(packages)
        return _run_npm_install(container_id, pkg_list)

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
