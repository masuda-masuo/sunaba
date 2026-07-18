"""Git root auto-detection for sandbox containers."""

from __future__ import annotations

import json
import shlex
from typing import Any

from sunaba.tools.common import LEGACY_WORKDIR

_DEFAULT_WD = LEGACY_WORKDIR


def resolve_git_root(
    container: Any,
    working_dir: str | None = None,
) -> str:
    """Return the container's git repository root.

    When *working_dir* is explicitly provided, return it unchanged.

    Otherwise the answer is simply the container's own working directory:
    :func:`sandbox_initialize` creates the container with the repo root as
    its ``WorkingDir``, so the path is already recorded in the container's
    config and no command has to run inside the container to find it.

    Containers created before that (``WorkingDir`` still the home directory)
    keep the old probe: metadata file, then home, then ``/tmp/repo/*``.
    """
    if working_dir is not None:
        return working_dir

    # The repo root as decided by the host at creation time.  Reading it back
    # from the container config costs no exec -- and, unlike the probe below,
    # cannot disagree with where the exec tools actually run, because it *is*
    # the directory they run in.
    config_wd = (container.attrs.get("Config") or {}).get("WorkingDir") or ""
    if config_wd and config_wd != LEGACY_WORKDIR:
        return str(config_wd)

    return _resolve_git_root_legacy(container)


def _resolve_git_root_legacy(container: Any) -> str:
    """Probe a pre-workspace container for its git root.

    Only reached for containers whose ``WorkingDir`` is still the home
    directory, i.e. ones created before the workspace became the repo root.
    """
    # Step 0: container metadata — written by sandbox_initialize after clone
    ec0, out0 = container.exec_run(
        ["/bin/sh", "-c",
         "cat /home/sandbox/.sandbox-meta.json 2>/dev/null || echo __NO_META__"],
        stdout=True,
    )
    if ec0 == 0:
        _stdout0, _ = (out0 if isinstance(out0, tuple) else (out0, b""))
        meta_str = _stdout0.decode("utf-8", errors="replace").strip() if _stdout0 else ""
        if meta_str and meta_str != "__NO_META__":
            try:
                meta = json.loads(meta_str)
                clone_path = meta.get("clone_path", "")
                if clone_path:
                    ec_ck, out_ck = container.exec_run(
                        ["/bin/sh", "-c",
                         f"cd {shlex.quote(clone_path)} && git rev-parse --show-toplevel 2>/dev/null || echo __NO_REPO__"],
                        stdout=True,
                    )
                    if ec_ck == 0:
                        _stdout_ck, _ = (out_ck if isinstance(out_ck, tuple) else (out_ck, b""))
                        verified = _stdout_ck.decode("utf-8", errors="replace").strip() if _stdout_ck else ""
                        if verified and verified != "__NO_REPO__":
                            return verified
            except json.JSONDecodeError:
                pass

    # Step 1: test the default location
    ec, out = container.exec_run(
        ["/bin/sh", "-c",
         "cd /home/sandbox && git rev-parse --show-toplevel 2>/dev/null || echo __NO_REPO__"],
        stdout=True,
    )
    if ec == 0:
        _stdout, _ = (out if isinstance(out, tuple) else (out, b""))
        path = _stdout.decode("utf-8", errors="replace").strip() if _stdout else ""
        if path and path != "__NO_REPO__":
            return path

    # Step 2: scan /tmp/repo/ for cloned repositories
    ec2, out2 = container.exec_run(
        ["/bin/sh", "-c",
         "for d in /tmp/repo/*/; do"
         '  [ -d "${d}.git" ] &&'
         "  git -C \"$d\" rev-parse --show-toplevel 2>/dev/null && exit 0;"
         "done; echo __NO_REPO__"],
        stdout=True,
    )
    if ec2 == 0:
        _stdout2, _ = (out2 if isinstance(out2, tuple) else (out2, b""))
        _path2 = _stdout2.decode("utf-8", errors="replace").strip() if _stdout2 else ""
        if _path2 and _path2 != "__NO_REPO__":
            return _path2

    return _DEFAULT_WD  # fallback
