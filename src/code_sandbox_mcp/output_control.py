"""Output control and sanitization for command execution results.

Provides:
- Verbosity levels (``error_only``, ``summary``, ``full``)
- Truncation with metadata (``shown``, ``total_lines``, ``truncated``)
- Paging (``offset``, ``limit``, ``next_offset``, ``has_more``)
- ANSI escape code stripping
- ``\\r`` progress bar collapsing
- Same-line consecutive output compression (``[\u00d7N] content``)
- Error-context-aware output (more lines near errors)

Usage::

    from code_sandbox_mcp.output_control import (
        sanitize_output, compress_repeated_lines,
        truncate_output, paginate_output,
    )

    clean = sanitize_output(raw_text)
    compressed = compress_repeated_lines(clean)
    display, meta = truncate_output(compressed, max_lines=50)
    page = paginate_output(display, offset=0, limit=20)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

#: ANSI escape sequences (colors, bold, cursor movement).
_ANSI_PATTERN: re.Pattern[str] = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

#: ISO-8601 timestamps commonly prepended by container logging drivers.
_TIMESTAMP_PATTERN: re.Pattern[str] = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[\.\,]\d+[Zz]? "
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class OutputMetadata:
    """Metadata about the output truncation state."""

    #: Number of lines actually shown in the truncated output.
    shown: int = 0

    #: Total number of lines in the original (untruncated) output.
    total_lines: int = 0

    #: Whether the output was truncated (fewer lines shown than total).
    truncated: bool = False


@dataclass
class PageResult:
    """A single page of paginated output with navigation metadata."""

    #: The page content (subset of lines).
    content: str = ""

    #: Offset for the next page, or ``None`` if this is the last page.
    next_offset: int | None = None

    #: Whether there are more pages after this one.
    has_more: bool = False


#: Valid verbose levels.
VerboseLevel = str  # "error_only" | "summary" | "full"


# ---------------------------------------------------------------------------
# Sanitization helpers
# ---------------------------------------------------------------------------


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from *text*.

    Strips colour codes, bold/italic, cursor movement, and other
    ANSI CSI sequences that are not meaningful in plain-text output.

    Args:
        text: Raw text possibly containing ANSI sequences.

    Returns:
        Clean text with all ANSI sequences removed.
    """
    return _ANSI_PATTERN.sub("", text)


def strip_carriage_returns(text: str) -> str:
    """Collapse ``\\r``-based progress bars, keeping only the final state.

    Many CLI tools (e.g. ``pip``, ``wget``) use ``\\r`` to update a
    single line in-place, creating progress indicators like::

        Downloading... 10%\\rDownloading... 20%\\rDownloading... 100%\\n

    This function keeps only the **last** value before each ``\\n``,
    so the output shows only the final state of each progress bar.

    Args:
        text: Raw text with embedded ``\\r`` characters.

    Returns:
        Clean text with only the final content after each ``\\r`` series.
    """
    lines = text.split("\n")
    cleaned: list[str] = []
    for line in lines:
        if "\r" in line:
            # Keep only the segment after the last \r
            parts = line.split("\r")
            cleaned.append(parts[-1])
        else:
            cleaned.append(line)
    return "\n".join(cleaned)


def strip_timestamps(text: str) -> str:
    """Remove ISO-8601 timestamps from the start of lines.

    Docker and other container logging drivers often prepend
    timestamps to each line like::

        2026-06-17T12:34:56.789Z command output here

    This function strips them for cleaner display.

    Args:
        text: Text with prepended timestamps.

    Returns:
        Text with timestamps removed from the start of each line.
    """
    return _TIMESTAMP_PATTERN.sub("", text)


def sanitize_output(text: str) -> str:
    """Clean raw output by removing ANSI codes, ``\\r`` bars, and timestamps.

    This is the main entry point for output sanitization.  It applies
    all three cleaners in sequence, mimicking ``CI=true`` / ``--no-color``
    forcing for containers that do not support those flags natively.

    Args:
        text: Raw output from the command.

    Returns:
        Clean, reader-friendly text.
    """
    result = strip_ansi(text)
    result = strip_carriage_returns(result)
    result = strip_timestamps(result)
    return result


def compress_repeated_lines(text: str) -> str:
    """Compress consecutive repeated lines into a single line with a counter.

    When the same line appears many times in a row (common in polling
    or retry loops), this collapses them::

        retrying...\nretrying...\nretrying...\n
        \u2192 [\u00d73] retrying...

    Args:
        text: Text with potential repeated consecutive lines.

    Returns:
        Text with consecutive duplicates collapsed.
    """
    lines = text.split("\n")
    if not lines:
        return text

    result: list[str] = []
    prev_line = lines[0]
    count = 1

    for line in lines[1:]:
        if line == prev_line:
            count += 1
        else:
            if count > 1:
                result.append(f"[\u00d7{count}] {prev_line}")
            else:
                result.append(prev_line)
            prev_line = line
            count = 1

    # Handle the last group
    if count > 1:
        result.append(f"[\u00d7{count}] {prev_line}")
    else:
        result.append(prev_line)

    return "\n".join(result)


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


def truncate_output(
    text: str,
    max_lines: int = 100,
    verbose: VerboseLevel = "summary",
    exit_code: int = 0,
    stderr: str = "",
) -> tuple[str, OutputMetadata]:
    """Truncate *text* to *max_lines* lines based on verbosity level.

    Args:
        text: The output text to truncate.
        max_lines: Maximum number of lines to show (must be >= 1).
        verbose: Verbosity level:

            - ``"error_only"``: Show output only if ``exit_code != 0`` or
              ``stderr`` is non-empty.  When shown, includes extra context
              around the error (tail of output, where errors typically are).
            - ``"summary"``: Show first few and last few lines with
              ``"... (N lines omitted)"`` in the middle (default).
            - ``"full"``: Show all output without truncation.

        exit_code: The process exit code (0 = success).
        stderr: Any stderr output from the process.

    Returns:
        Tuple of ``(truncated_content, metadata)`` where *metadata*
        contains ``shown``, ``total_lines``, and ``truncated``.
    """
    # Guard against non-positive max_lines
    if max_lines < 1:
        max_lines = 1

    if not text.strip():
        return "", OutputMetadata(shown=0, total_lines=0, truncated=False)

    lines = text.split("\n")
    total_lines = len(lines)
    metadata = OutputMetadata(total_lines=total_lines)

    if verbose == "error_only":
        if exit_code == 0 and not stderr:
            return "", OutputMetadata(
                shown=0, total_lines=total_lines, truncated=False
            )
        # When showing errors, include more context (tail of output)
        return _truncate_tail(lines, max_lines, metadata)

    if verbose == "full":
        metadata.shown = total_lines
        metadata.truncated = False
        return text, metadata

    # summary mode (default)
    if total_lines <= max_lines:
        metadata.shown = total_lines
        return text, metadata

    # Show first N/2 lines and last N/2 lines
    head_count = max_lines // 2
    tail_count = max_lines - head_count

    head = lines[:head_count]
    tail = lines[-tail_count:]
    omitted = total_lines - max_lines
    shown_lines = head + [f"... ({omitted} lines omitted)"] + tail
    metadata.shown = len(shown_lines)
    metadata.truncated = True

    return "\n".join(shown_lines), metadata


def _truncate_tail(
    lines: list[str],
    max_lines: int,
    metadata: OutputMetadata,
) -> tuple[str, OutputMetadata]:
    """Return the last *max_lines* lines (tail-focused truncation).

    Used by ``error_only`` mode to show context around errors, which
    typically appear at the end of the output.
    """
    total = len(lines)
    if total <= max_lines:
        metadata.shown = total
        return "\n".join(lines), metadata

    tail_lines = lines[-max_lines:]
    metadata.shown = max_lines
    metadata.truncated = True
    return "\n".join(tail_lines), metadata


# ---------------------------------------------------------------------------
# Paging
# ---------------------------------------------------------------------------


def paginate_output(
    text: str,
    offset: int = 0,
    limit: int = 50,
) -> PageResult:
    """Return a page of *text* starting at *offset* with *limit* lines.

    Args:
        text: Full text content to paginate.
        offset: Line offset to start from (0-indexed).
        limit: Maximum number of lines to include in this page.

    Returns:
        A :class:`PageResult` with the page content and navigation
        metadata (``next_offset``, ``has_more``).
    """
    lines = text.split("\n")
    total = len(lines)

    if offset >= total:
        return PageResult(content="", has_more=False)

    page_lines = lines[offset:offset + limit]
    next_offset = offset + limit
    has_more = next_offset < total

    return PageResult(
        content="\n".join(page_lines),
        next_offset=next_offset if has_more else None,
        has_more=has_more,
    )
