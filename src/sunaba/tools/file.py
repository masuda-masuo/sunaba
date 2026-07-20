"""File tools: write_file, edit_file, transform_file, undo_file_edit, copy_project, copy_file, read_file_range, list_files."""

from __future__ import annotations

import difflib
import io
import json
import logging
import os
import posixpath
import shlex
import subprocess
import tarfile
import tempfile
from pathlib import Path

from docker.errors import APIError, NotFound

from sunaba import undo
from sunaba.edit_verify import (
    _file_size_from_counts,
    edit_symbol_in_container,
    read_file,
    read_file_lines,
    transform_file_in_container,
)
from sunaba.edit_verify import (
    write_file as write_file_in_container,
)
from sunaba.journal import record_copy, record_tool_use
from sunaba.output_control import paginate_output, truncate_output
from sunaba.tools.common import WORKSPACE, _docker, container_not_found_error
from sunaba.tools.edit_engine import (
    _ALREADY_APPLIED_MIN_CHARS,
    _build_near_miss_echo,
    _build_success_echo,
    _extract_symbol_from_old_str,
    _find_all_matches,
    _is_bare_signature,
    _parses_as_definition,
    _python_syntax_note,
    _try_whitespace_flexible,
)

logger: logging.Logger = logging.getLogger(__name__)

def write_file(
    container_id: str,
    file_name: str,
    file_contents: str,
    dest_dir: str = WORKSPACE,
) -> str:
    """Create a file, or fully overwrite an existing one.

    The file becomes exactly file_contents.  This tool never does
    partial updates: to change part of an existing file (string
    replace, line range, append) use edit_file; for bulk or computed
    edits use transform_file.

    An existing file's pre-write content is snapshotted first;
    undo_file_edit restores it.  On .py files, content that does not
    parse is flagged with a warning in the echo.

    Args:
        container_id: Container ID prefix.
        file_name: Name of the file to write.
        file_contents: Complete new content of the file.
        dest_dir: Destination directory.

    Returns:
        Success or error message.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"
    except Exception as e:
        return f"Error: {e}"

    dest_path = posixpath.join(dest_dir, file_name)

    syntax_note = _python_syntax_note(dest_path, file_contents)

    # Snapshot the pre-write content so undo_file_edit can restore it.
    try:
        existing: str | None = read_file(container, dest_path)
    except ValueError:
        existing = None  # new file -- nothing to snapshot
    if existing is not None:
        undo.save_version(container_id, dest_path, existing)

    # overwrote_existing feeds the issue #630 measurement: how often a
    # full overwrite hits a file that edit_file could have modified.
    record_tool_use(
        container_id[:12],
        "write_file",
        {"file_path": dest_path, "overwrote_existing": existing is not None},
    )

    try:
        write_file_in_container(container, container_id[:12], dest_path, file_contents)
    except ValueError as e:
        return f"Error: {e}"

    return f"Written {len(file_contents)} bytes to {dest_path}" + syntax_note


def edit_file(
    container_id: str,
    file_name: str,
    file_contents: str,
    dest_dir: str = WORKSPACE,
    start_line: int | None = None,
    end_line: int | None = None,
    append: bool = False,
    old_str: str | None = None,
    preserve: str | None = None,
    line: int | None = None,
    ast: bool | None = None,
) -> str:
    """Edit part of an existing file in the container.

    Exactly one edit mode is required: string replace (old_str),
    line-range (start_line[/end_line], 1-indexed inclusive), or
    append=True.  The file must already exist -- to create a file or
    replace one wholesale use write_file.

    old_str is designed for small, targeted replacements -- keep it
    minimal and unique.  It matches the exact string you provide (a
    multi-line match is replaced whole).  Multiple matches are
    rejected with their line numbers; an inexact match retries with
    per-line whitespace stripped and re-indents on success; no match
    returns the nearest-miss region (and says so when file_contents
    is already in the file -- probably applied by an earlier call).
    A successful replace echoes the post-edit region with line
    numbers; a .py edit that leaves the file unparseable is flagged
    there.

    To replace a whole Python function/class, pass its signature as
    old_str (e.g. ``def foo():``) -- it resolves via AST; a no-op
    returns "No changes" and a resolution failure is surfaced when
    file_contents is a complete definition (no silent fallback).

    For large blocks prefer:
      .py: pass ``def foo():`` for AST resolution (no multi-line match).
      Any: use ``start_line``/``end_line`` for line-range replacement.

    Every edit snapshots the pre-edit file; undo_file_edit restores it
    -- prefer that over repairing a broken file in place.  For bulk or
    computed edits use transform_file.

    Args:
        container_id: Container ID prefix.
        file_name: Name of the file to edit.
        file_contents: Replacement text for the chosen mode.
        dest_dir: Destination directory.
        start_line: Line-range start (1-indexed, inclusive).
        end_line: Line-range end (1-indexed, inclusive; default last line).
        append: Append to the end of the existing file.
        old_str: Exact text to replace with file_contents (matching
            contract above).  For small replacements only -- large
            blocks: use ``def foo():`` (.py, AST) or
            ``start_line``/``end_line`` (any file).
        preserve: For old_str AST resolution on .py files, parts of
            the old definition to keep: ``"decorators+docstring"``
            (default), ``"decorators"``, ``"docstring"``, or
            ``"none"``.
        line: For old_str AST resolution on .py files, disambiguates
            same-name definitions (any line inside the target).
        ast: Overrides the implicit old_str AST trigger on .py files.
            ``True`` forces AST resolution (error, no fallback).
            ``False`` forces a plain string replace even for a
            def/class old_str (e.g. docstring-only edits).

    Returns:
        Success or error message.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"
    except Exception as e:
        return f"Error: {e}"

    dest_path = posixpath.join(dest_dir, file_name)

    # Exactly one edit mode -- no mode is not a full overwrite here,
    # that is write_file's job (issue #630: the split is by intent,
    # and any overlap would re-create the old tool-shadowing problem).
    has_line_range = start_line is not None or end_line is not None
    mode_count = sum([append, old_str is not None, has_line_range])
    if mode_count > 1:
        return "Error: start_line/end_line, append, and old_str are mutually exclusive"
    if mode_count == 0:
        return (
            "Error: edit_file requires one edit mode: old_str, "
            "start_line/end_line, or append=True. To create a new file "
            "or fully overwrite one use write_file."
        )

    if old_str is not None and old_str == "":
        return "Error: old_str must not be empty"
    if start_line is not None and start_line < 1:
        return "Error: start_line must be >= 1"

    try:
        existing = read_file(container, dest_path)
    except ValueError:
        return (
            f"Error: file {dest_path} not found. edit_file only "
            "modifies existing files; use write_file to create it."
        )
    existing_lines = existing.splitlines()

    # Validate bounds
    if start_line is not None and start_line > len(existing_lines):
        return f"Error: start_line {start_line} exceeds file length ({len(existing_lines)} lines)"
    if end_line is not None:
        if end_line > len(existing_lines):
            return f"Error: end_line {end_line} exceeds file length ({len(existing_lines)} lines)"
        if start_line is not None and start_line > end_line:
            return "Error: start_line is greater than end_line"

    record_tool_use(
        container_id[:12],
        "edit_file",
        {
            "file_path": dest_path,
            "mode": (
                "old_str" if old_str is not None
                else "append" if append
                else "line_range"
            ),
        },
    )

    content = file_contents
    # 1-indexed (start, end) lines of the replaced region in the new
    # content; set only for successful old_str edits (issue #580).
    replaced_span: tuple[int, int] | None = None

    if append:
        sep = "\n" if existing else ""
        content = existing.rstrip("\n") + sep + file_contents
        # rstrip() would also swallow the file's final newline (#570).
        if existing.endswith("\n") and not content.endswith("\n"):
            content += "\n"
    elif old_str is not None:
        symbol: str | None = None
        ast_error: str | None = None
        if ast is True and not dest_path.endswith(".py"):
            return "Error: ast=True requires a .py file"
        attempt_ast = ast is not False and dest_path.endswith(".py")
        if attempt_ast:
            symbol = _extract_symbol_from_old_str(old_str)
            if symbol is None and ast is True:
                return (
                    "Error: ast=True requires old_str to start with a "
                    "function/class definition (a `def`/`async def`/`class` "
                    "line, optionally preceded by decorators/comments)."
                )
            if symbol is not None:
                ast_result = edit_symbol_in_container(
                    client, container_id, dest_path, symbol, file_contents, line, preserve or "decorators+docstring",
                )
                if ast_result.get("status") == "ok":
                    resolved = ast_result.get("resolved", {})
                    if ast_result.get("changed"):
                        undo.save_version(container_id, dest_path, existing)
                        try:
                            new_content = read_file(container, dest_path)
                        except ValueError:
                            return f"Error: failed to read {dest_path} after edit"
                        rep_start = resolved.get("start_line", 1)
                        rep_end = resolved.get("end_line", 1)
                        return _build_success_echo(new_content, dest_path, rep_start, rep_end)
                    # AST no-op: the resolved definition already matches
                    # file_contents.  Never fall through to string
                    # matching here -- old_str would re-match the
                    # signature line and splice a duplicate body into
                    # the file.
                    span = ""
                    if resolved.get("start_line") and resolved.get("end_line"):
                        span = f" (lines {resolved['start_line']}-{resolved['end_line']})"
                    return (
                        f"No changes to {dest_path}: "
                        f"{resolved.get('kind', 'definition')} "
                        f"'{resolved.get('qualname', symbol)}'{span} "
                        "already matches file_contents"
                    )
                ast_error = ast_result.get("error", "AST resolution failed")
                if ast is True:
                    return f"Error: {ast_error}"
                if (
                    _parses_as_definition(file_contents)
                    and _is_bare_signature(old_str)
                ):
                    # old_str is a bare signature and file_contents a
                    # complete definition: the string fallback would
                    # replace only the signature line and orphan the
                    # old body.  Surface the AST error instead.
                    return (
                        f"{ast_error}\n"
                        f"Note: old_str looks like a bare '{symbol}' "
                        "signature and file_contents is a complete "
                        "definition, so this edit must go through AST "
                        "resolution (a plain string replacement would "
                        "leave the old body behind). Fix the error "
                        "above, put the full old definition in "
                        "old_str for an exact string edit, or use "
                        "transform_file."
                    )
                logger.debug(
                    "AST resolution attempted for %s (symbol=%s) but failed: %s"
                    " -- falling through to string matching",
                    dest_path, symbol, ast_error,
                )

        # 1. Exact match with uniqueness check
        exact_matches = _find_all_matches(existing, old_str)
        if len(exact_matches) > 1:
            line_nos = ", ".join(str(m[1]) for m in exact_matches[:10])
            suffix = "..." if len(exact_matches) > 10 else ""
            return (
                f"Error: old_str matches at {len(exact_matches)} locations "
                f"(lines {line_nos}{suffix}). "
                "Add more surrounding context to make it unique, or use "
                "transform_file to edit several occurrences in one call."
            )
        if len(exact_matches) == 1:
            idx = exact_matches[0][0]
            content = (
                existing[:idx]
                + file_contents
                + existing[idx + len(old_str) :]
            )
            rep_start = existing[:idx].count("\n") + 1
            end_offset = idx + len(file_contents)
            rep_end = content[:end_offset].count("\n") + 1
            if file_contents.endswith("\n") and file_contents:
                rep_end -= 1
            replaced_span = (rep_start, max(rep_start, rep_end))
        else:
            # 2. Whitespace-flexible fallback
            result = _try_whitespace_flexible(
                existing, old_str, file_contents,
            )
            if isinstance(result, str):
                return result  # ambiguous-match error
            if result is not None:
                content, rep_start, rep_end = result
                replaced_span = (rep_start, rep_end)
            else:
                # 3. Near-miss echo
                near_miss = _build_near_miss_echo(existing, old_str, dest_path)
                # A retried edit is the most common cause of "old_str
                # not found": the previous call already replaced it.
                # Saying so breaks the re-read/retry loop early.
                if (
                    len(file_contents.strip()) >= _ALREADY_APPLIED_MIN_CHARS
                    and file_contents in existing
                ):
                    line_no = existing[: existing.find(file_contents)].count("\n") + 1
                    near_miss += (
                        f"\nNote: file_contents already appears at line "
                        f"{line_no} -- this edit may have already been "
                        "applied. Re-read the file before retrying."
                    )
                if ast_error is not None:
                    near_miss += (
                        f"\nNote: AST resolution for '{symbol}' was "
                        f"attempted first and failed: {ast_error}"
                    )
                return near_miss
    else:
        start = start_line - 1 if start_line is not None else 0
        end = end_line if end_line is not None else len(existing_lines)
        new_lines = file_contents.splitlines()
        content_lines = existing_lines[:start] + new_lines + existing_lines[end:]
        content = "\n".join(content_lines)
        # The trailing newline belongs to the file, not to the replacement
        # snippet: splitlines() drops it, so restore it from *existing*
        # (#570).  A snippet that ends in "\n" still forces one, so a file
        # that lacked the final newline can gain it deliberately.
        if existing.endswith("\n") or file_contents.endswith("\n"):
            content += "\n"

    syntax_note = _python_syntax_note(dest_path, content)

    # Snapshot the pre-edit content so undo_file_edit can restore it.
    undo.save_version(container_id, dest_path, existing)

    try:
        write_file_in_container(container, container_id[:12], dest_path, content)
    except ValueError as e:
        return f"Error: {e}"
    if replaced_span is not None:
        return _build_success_echo(content, dest_path, *replaced_span) + syntax_note
    return f"Written {len(content)} bytes to {dest_path}" + syntax_note


# ---------------------------------------------------------------------------
# undo_file_edit
# ---------------------------------------------------------------------------

# Max diff lines echoed by undo_file_edit before truncation.
_UNDO_DIFF_MAX_LINES = 50


def undo_file_edit(
    container_id: str,
    file_path: str,
    steps: int = 1,
) -> str:
    """Restore *file_path* to the state it had before a recent edit.

    Every write_file / edit_file / transform_file edit snapshots the
    pre-edit file automatically, so a broken edit is never a dead end:
    call this to step back to the file as it was BEFORE the edit,
    instead of trying to repair broken text in place.  steps=1 (default)
    is the state right before the last edit; steps=2 the edit before
    that, and so on.

    The current content is snapshotted too before restoring, so an
    undo can itself be undone: calling again with steps=1 re-applies
    the undone edit (redo).

    Args:
        container_id: Container ID prefix.
        file_path: Absolute path of the file inside the container
            (the same path echoed by the editing tools).
        steps: How many edits to step back (default 1).

    Returns:
        JSON: status, file_path, restored diff (capped), and the
        remaining snapshots; error with available snapshots when
        no matching snapshot exists.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return container_not_found_error(container_id)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

    target = undo.get_version(container_id, file_path, steps)
    if target is None:
        available = undo.list_versions(container_id, file_path)
        if not available:
            return json.dumps({
                "status": "error",
                "error": (
                    f"No undo history for {file_path}. Snapshots are taken "
                    "on every write_file/edit_file/transform_file edit in "
                    "this server session; pass the exact path echoed by "
                    "those tools."
                ),
            })
        return json.dumps({
            "status": "error",
            "error": (
                f"steps={steps} is out of range for {file_path}: "
                f"{len(available)} snapshot(s) available."
            ),
            "snapshots": available,
        })

    try:
        current = read_file(container, file_path)
    except ValueError:
        current = None

    if current is not None:
        undo.save_version(container_id, file_path, current)

    try:
        write_file_in_container(container, container_id[:12], file_path, target)
    except ValueError as e:
        return json.dumps({"status": "error", "error": str(e)})

    record_tool_use(
        container_id[:12],
        "undo_file_edit",
        {"file_path": file_path, "steps": steps},
    )

    diff_lines = list(difflib.unified_diff(
        (current or "").splitlines(),
        target.splitlines(),
        fromfile=f"{file_path} (before undo)",
        tofile=f"{file_path} (restored)",
        lineterm="",
    ))
    truncated = len(diff_lines) > _UNDO_DIFF_MAX_LINES
    if truncated:
        remaining = len(diff_lines) - _UNDO_DIFF_MAX_LINES
        diff_lines = diff_lines[:_UNDO_DIFF_MAX_LINES] + [
            f"... (truncated, {remaining} more lines)"
        ]

    return json.dumps({
        "status": "ok",
        "file_path": file_path,
        "restored_steps_back": steps,
        "diff": "\n".join(diff_lines),
        "note": (
            "The replaced content was snapshotted too: undo_file_edit "
            "with steps=1 now re-applies the undone edit (redo); "
            "steps=2 goes further back."
        ),
        "snapshots": undo.list_versions(container_id, file_path),
    })


# ---------------------------------------------------------------------------
# copy_project
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Helpers for copy_project / copy_file
# ---------------------------------------------------------------------------

_GIT_TIMEOUT = 30  # seconds for git subprocess calls


def _walk_files(base_path: Path) -> list[str]:
    """Walk *base_path* and return all file paths relative to it."""
    files: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(str(base_path)):
        for fn in filenames:
            rel = os.path.relpath(os.path.join(dirpath, fn), str(base_path))
            files.append(rel)
    return files


def _get_container_user_uidgid(container) -> tuple[int, int]:
    """Return (uid, gid) of the container's default user.

    Raises ``RuntimeError`` when the information cannot be obtained.
    """
    uid_ec, uid_b = container.exec_run(["id", "-u"])
    gid_ec, gid_b = container.exec_run(["id", "-g"])
    if uid_ec != 0 or gid_ec != 0:
        raise RuntimeError(
            f"id -u returned exit {uid_ec}, id -g returned exit {gid_ec}"
        )
    try:
        uid = int(uid_b.decode("utf-8").strip())
        gid = int(gid_b.decode("utf-8").strip())
    except (ValueError, TypeError) as e:
        raise RuntimeError(f"Could not parse container user id: {e}") from e
    return uid, gid


def _normalize_ownership(container, dest_path: str) -> str | None:
    """Chown *dest_path* so the container user owns everything.

    Returns ``None`` on success, or an error message on failure.
    """
    try:
        uid, gid = _get_container_user_uidgid(container)
    except Exception as e:
        return f"Error: Failed to determine container user id: {e}"

    try:
        chown_ec, chown_out = container.exec_run(
            ["chown", "-R", f"{uid}:{gid}", dest_path],
            user="root",
        )
    except Exception as e:
        return (
            f"Error: Failed to set ownership on {dest_path}: {e}. "
            f"Files were copied to the container but may not be writable. "
            f"Run 'chown -R {uid}:{gid} {dest_path}' as root inside the container."
        )

    if chown_ec != 0:
        return (
            f"Error: Failed to set ownership on {dest_path} (exit {chown_ec}): "
            f"{chown_out.decode('utf-8').strip()}. "
            f"Files were copied to the container but may not be writable. "
            f"Run 'chown -R {uid}:{gid} {dest_path}' as root inside the container."
        )

    return None


# ---------------------------------------------------------------------------
# copy_project
# ---------------------------------------------------------------------------


def copy_project(
    container_id: str,
    local_src_dir: str,
    dest_dir: str = WORKSPACE,
    include_untracked: bool = False,
) -> str:
    """Copy a local directory into the container as a tar archive.

    By default only git-tracked files (plus ``.git/``) are transferred.
    Untracked and gitignored files are left behind.  Pass
    ``include_untracked=True`` to opt in to copying untracked files too.

    The directory's *contents* land in *dest_dir*, so the copied project
    becomes the git root the container already works in -- verify and publish
    find it without being told where it is.

    .. note::

       After copying, every file is ``chown``-ed to the container's running
       user so they remain writable by the file-editing tools.

    Args:
        container_id: 12-character container ID prefix.
        local_src_dir: Path to the local directory to copy.
        dest_dir: Destination directory in the container (default: the
            workspace, ``/workspace``).
        include_untracked: If ``True``, untracked files are copied alongside
            tracked ones.  Gitignored files are still left behind (the
            enumeration honours ``.gitignore``), so build output and virtual
            environments do not come along.  For a directory that is not a git
            repository at all, this flag is required and copies it wholesale.
            Default ``False``.

    Returns:
        Success or error message.

    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"
    except Exception as e:
        return f"Error: {e}"

    src_path = Path(local_src_dir).resolve()
    if not src_path.exists():
        return f"Error: {local_src_dir} does not exist"
    if not src_path.is_dir():
        return f"Error: {local_src_dir} is not a directory"

    # -- Decide whether this is a git repository ---------------------------------
    git_dir = src_path / ".git"
    is_git_repo = git_dir.exists()

    if is_git_repo and git_dir.is_file():
        # File-based .git (git worktree / submodule worktree).
        # The pointer would resolve to a path that does not exist inside the
        # container, producing a broken repository.  Refuse.
        return (
            f"Error: {local_src_dir} has a file-based .git pointer "
            f"(worktree checkout or submodule worktree). "
            f"copy_project cannot copy this reliably because the gitdir "
            f"pointer resolves to a host-only path. "
            f"Use a non-worktree clone instead."
        )

    # -- Build file list ---------------------------------------------------------
    tracked_files: list[str] = []
    untracked_files: list[str] = []

    if is_git_repo:
        try:
            # Tracked files (all states in the index), with stage info so we
            # can filter out submodule gitlinks (mode 160000).
            proc = subprocess.run(
                ["git", "ls-files", "--cached", "--stage"],
                cwd=src_path,
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return "Error: git ls-files timed out (possible stalled filesystem)"
        except FileNotFoundError:
            return "Error: git not found in PATH"

        if proc.returncode != 0:
            return f"Error: git ls-files failed: {proc.stderr.strip()}"

        for raw_line in proc.stdout.strip().split("\n"):
            line = raw_line.strip()
            if not line:
                continue
            # Format: "100644 <sha> <stage>\t<path>" or "160000 <sha> <stage>\t<path>"
            meta_part, _, filepath = line.partition("\t")
            if not filepath:
                continue
            mode = meta_part.split(maxsplit=1)[0] if meta_part else ""
            if mode == "160000":
                # Submodule gitlink -- do not recurse into it
                continue
            tracked_files.append(filepath)

        # Always enumerate untracked files for reporting.
        try:
            proc2 = subprocess.run(
                ["git", "ls-files", "--others", "--exclude-standard"],
                cwd=src_path,
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return "Error: git ls-files --others timed out"
        except FileNotFoundError:
            return "Error: git not found in PATH"
        if proc2.returncode == 0:
            untracked_files = [
                f for f in proc2.stdout.strip().split("\n") if f.strip()
            ]
    elif include_untracked:
        # Not a git repository: there is no notion of "tracked" here, so the
        # only honest options are to copy everything or to refuse.  The caller
        # has explicitly opted in, so copy everything.
        tracked_files = _walk_files(src_path)
    else:
        # Not a git repository and no opt-in.  Copying everything is exactly
        # the incident this issue exists to prevent (#678), and without git we
        # cannot tell an operator's private files from the project's own.
        # Refuse rather than silently degrading the guarantee.
        return (
            f"Error: {local_src_dir} is not a git repository, so copy_project "
            f"cannot tell tracked files from untracked ones. Refusing rather "
            f"than copying everything, which risks transferring private files. "
            f"Pass include_untracked=True to copy the directory wholesale."
        )

    # -- Build count summary for the result message ------------------------------
    tracked_count = len(tracked_files)
    untracked_count = len(untracked_files)
    skipped_untracked_count = untracked_count if not include_untracked else 0
    total = tracked_count + (untracked_count if include_untracked else 0)

    # -- Create tar archive ------------------------------------------------------
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".tar")
    try:
        with tarfile.open(fileobj=tmp.file, mode="w") as tar:
            if is_git_repo:
                # Include the .git metadata directory
                tar.add(str(git_dir), arcname=".git")
            for filepath in tracked_files:
                full_path = src_path / filepath
                if full_path.exists():
                    tar.add(str(full_path), arcname=f"./{filepath}")
            if include_untracked:
                for filepath in untracked_files:
                    full_path = src_path / filepath
                    if full_path.exists():
                        tar.add(str(full_path), arcname=f"./{filepath}")
        tmp.file.close()

        # -- Transfer into container ---------------------------------------------
        with open(tmp.name, "rb") as f:
            data = f.read()
        buf = io.BytesIO(data)
        try:
            container.put_archive(dest_dir, buf)
        except APIError as e:
            return f"Error: {e}"

        # -- Normalise ownership --------------------------------------------------
        own_err = _normalize_ownership(container, dest_dir)
        if own_err is not None:
            # Files have already landed; the caller must fix ownership manually.
            return own_err

        record_copy(
            container_id[:12], "copy_project", local_src_dir, dest_dir
        )

        # -- Build success message ------------------------------------------------
        summary_parts: list[str] = []
        if is_git_repo:
            summary_parts.append(f"{tracked_count} tracked files")
            if include_untracked and untracked_count:
                summary_parts.append(f"{untracked_count} untracked files")
            elif skipped_untracked_count:
                summary_parts.append(f"{skipped_untracked_count} untracked skipped")
        else:
            summary_parts.append(f"{total} files (not a git repository)")

        return (
            f"Copied {local_src_dir} to {dest_dir} "
            f"in container {container_id[:12]} "
            f"({'; '.join(summary_parts)})"
        )
    finally:
        os.unlink(tmp.name)


# ---------------------------------------------------------------------------
# copy_file
# ---------------------------------------------------------------------------


def copy_file(
    container_id: str,
    local_src_file: str,
    dest_path: str = WORKSPACE,
) -> str:
    """Copy a single local file into the container.

    After copying, the file is ``chown``-ed to the container's running
    user so it remains writable by the file-editing tools.

    Args:
        container_id: 12-character container ID prefix.
        local_src_file: Path to the local file to copy.
        dest_path: Destination directory or path in the container
            (default: the workspace, ``/workspace``).

    Returns:
        Success or error message.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"
    except Exception as e:
        return f"Error: {e}"

    src = Path(local_src_file).resolve()
    if not src.exists():
        return f"Error: {local_src_file} does not exist"
    if not src.is_file():
        return f"Error: {local_src_file} is not a file"

    dest = dest_path
    if not dest.endswith("/") and not dest.endswith(src.name):
        dest = posixpath.join(dest_path, src.name)

    parent_dir = posixpath.dirname(dest)
    base_name = posixpath.basename(dest)

    with open(src, "rb") as f:
        data = f.read()

    tar_stream = io.BytesIO()
    with tarfile.open(fileobj=tar_stream, mode="w") as tar:
        info = tarfile.TarInfo(name=base_name)
        info.size = len(data)
        info.mtime = int(src.stat().st_mtime)
        tar.addfile(info, io.BytesIO(data))

    try:
        container.put_archive(parent_dir, tar_stream.getvalue())
    except APIError as e:
        return f"Error: {e}"

    own_err = _normalize_ownership(container, dest)
    if own_err is not None:
        return own_err

    record_copy(container_id[:12], "copy_file", local_src_file, dest)
    return f"Copied {local_src_file} to {dest} in container {container_id[:12]}"


# ---------------------------------------------------------------------------
# read_file_range
# ---------------------------------------------------------------------------


def read_file_range(
    container_id: str,
    file_path: str,
    offset: int = 0,
    limit: int = 50,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    """Read lines from *file_path* inside the container.

    Args:
        container_id: Container ID prefix.
        file_path: File path inside the container.
        offset: 0-indexed start line.
        limit: Max lines to return; -1 reads to end of file.
        start_line: 1-indexed inclusive start. start_line/end_line and
            offset/limit are mutually exclusive pairs.
        end_line: 1-indexed inclusive end; default end of file.

    Returns:
        JSON: content, total_lines, shown, has_more, next_offset.
    """
    client = _docker()
    try:
        _ = client.containers.get(container_id)
    except NotFound:
        return container_not_found_error(container_id)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

    if start_line is not None and offset != 0:
        return json.dumps({
            "status": "error",
            "error": "start_line and offset are mutually exclusive. "
            "Use start_line/end_line (1-indexed) or offset/limit (0-indexed), not both."
        })
    if start_line is not None and start_line < 1:
        return json.dumps({"status": "error", "error": "start_line must be >= 1 (1-indexed)"})
    if end_line is not None and start_line is not None and end_line < start_line:
        return json.dumps({"status": "error", "error": "end_line must be >= start_line"})
    resolved_offset = offset
    resolved_limit = limit
    if start_line is not None:
        resolved_offset = start_line - 1
        if end_line is not None:
            resolved_limit = end_line - start_line + 1
        else:
            resolved_limit = -1
    record_tool_use(
        container_id[:12],
        "read_file_range",
        {"file_path": file_path},
    )
    result = read_file_lines(
        _, file_path, offset=resolved_offset, limit=resolved_limit
    )
    return json.dumps(result)


# ---------------------------------------------------------------------------
# list_files
# ---------------------------------------------------------------------------


def list_files(
    container_id: str,
    path: str = WORKSPACE,
    max_depth: int = 3,
    pattern: str = "",
) -> str:
    """List files inside the container using ``find``.

    Returns a JSON array of file paths sorted alphabetically.
    Hidden files (dotfiles) and directories under ``.git`` are
    excluded.

    Args:
        container_id: 12-character container ID prefix.
        path: Directory path to list (default: the workspace,
            ``"/workspace"``).
        max_depth: Maximum directory depth (default 3).
        pattern: Optional glob pattern to filter files
            (e.g. ``"*.py"``, ``"*.md"``).

    Returns:
        JSON string with ``path``, ``total``, and ``files`` list.
        On error returns an ``error`` field.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return container_not_found_error(container_id)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

    safe_path = shlex.quote(path)

    name_filter = ""
    if pattern:
        name_filter = f" -name {shlex.quote(pattern)}"

    cmd = (
        f"find {safe_path} -maxdepth {max_depth}"
        f" -not -path '*/\\.*'"
        f" -type f{name_filter}"
        f" | sort"
    )

    exit_code, output = container.exec_run(
        ["/bin/sh", "-c", cmd],
        stdout=True,
        stderr=True,
    )

    stdout_part, stderr_part = (
        output if isinstance(output, tuple) else (output, b"")
    )
    stdout_text = (
        stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    )
    stderr_text = (
        stderr_part.decode("utf-8", errors="replace") if stderr_part else ""
    )

    if exit_code != 0:
        return json.dumps({"status": "error", "error": stderr_text or stdout_text})

    record_tool_use(
        container_id[:12],
        "list_files",
        {"path": path, "max_depth": max_depth, "pattern": pattern},
    )
    files = [f for f in stdout_text.strip().split("\n") if f]
    return json.dumps({
        "path": path,
        "total": len(files),
        "files": files,
    })


# ---------------------------------------------------------------------------
# transform_file (moved from verify.py, issue #258)
# ---------------------------------------------------------------------------


def transform_file(
    container_id: str,
    file_path: str,
    code: str,
    max_lines: int = 200,
    offset: int = 0,
    limit: int = 100,
) -> str:
    """Edit a file by running Python that computes the new text.

    code executes as a complete module inside the container (never on
    the host); when it finishes, a top-level callable
    transform(text: str) -> str must exist (helpers and imports are
    fine).  The file's text goes in, the returned text is written back,
    and a unified diff is returned -- always check it: an over-broad
    pattern can change more than intended (the pre-edit file is
    snapshotted; undo_file_edit rolls it back).  Prefer
    edit_file old_str for a single known replacement; use
    this for bulk, pattern, or computed edits.  Example::

        import re
        def transform(text):
            return re.sub("todo", "TODO", text)

    Args:
        container_id: Container ID prefix.
        file_path: Absolute path inside the container.
        code: Python source defining transform(text) -> str. Transported
            base64-encoded, so quotes, backslashes, and newlines need no
            escaping -- write it exactly as a .py file.
        max_lines: Max diff lines shown.
        offset: Diff paging offset (0-indexed).
        limit: Max diff lines per page.

    Returns:
        JSON: status, changed, diff (paginated, with paging metadata);
        on failure error, and traceback when your code raised.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return container_not_found_error(container_id)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

    record_tool_use(
        container_id[:12],
        "transform_file",
        {"file_path": file_path},
    )
    # Pre-edit snapshot for undo_file_edit -- best-effort: the edit
    # must never fail because its undo snapshot could not be read.
    try:
        before = read_file(container, file_path)
    except Exception:
        before = None
    result = transform_file_in_container(client, container_id, file_path, code)

    if result.get("status") == "ok" and result.get("changed"):
        if before is not None:
            undo.save_version(container_id, file_path, before)

        display, meta = truncate_output(
            result.get("diff", ""),
            max_lines=max_lines,
            verbose="full",
        )
        page = paginate_output(display, offset=offset, limit=limit)
        return json.dumps({
            "status": "ok",
            "changed": True,
            "diff": page.content,
            "shown": meta.shown,
            "total_lines": meta.total_lines,
            "truncated": meta.truncated,
            "next_offset": page.next_offset,
            "has_more": page.has_more,
            "file_size": _file_size_from_counts(
                int(result.get("new_size", 0)), int(result.get("new_lines", 0))
            ),
        })

    # Unchanged (or error-free no-op) results still surface file_size so the
    # model sees the current size without a separate read (issue #187, ①).
    if result.get("status") == "ok" and not result.get("changed"):
        result["file_size"] = _file_size_from_counts(
            int(result.get("new_size", 0)), int(result.get("new_lines", 0))
        )
    return json.dumps(result)
