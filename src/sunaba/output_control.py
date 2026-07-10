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

    from sunaba.output_control import (
        sanitize_output, compress_repeated_lines,
        truncate_output, paginate_output,
    )

    clean = sanitize_output(raw_text)
    compressed = compress_repeated_lines(clean)
    display, meta = truncate_output(compressed, max_lines=50)
    page = paginate_output(display, offset=0, limit=20)
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

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


#: Pattern matching VCS token environment variable assignments.
#: Captures the variable name (group 1) so the replacement preserves
#: the key while masking the secret value.
_TOKEN_MASK_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(GITHUB_TOKEN=)['\"]?[^\s'\"]+['\"]?"),
    re.compile(r"(GH_TOKEN=)['\"]?[^\s'\"]+['\"]?"),
    re.compile(r"(GITHUB_TOKEN_SOURCE=)['\"]?[^\s'\"]+['\"]?"),
]


def mask_tokens(text: str) -> str:
    """Mask VCS authentication token values in command output.

    Replaces the token value in ``KEY=value`` patterns with ``***``
    so that credentials are not leaked through execution logs,
    AI context, or journal records.

    Args:
        text: Raw output that may contain token assignments.

    Returns:
        Text with token values masked.
    """
    result = text
    for pattern in _TOKEN_MASK_PATTERNS:
        result = pattern.sub(r"\1***", result)
    return result


def sanitize_output(text: str) -> str:
    """Clean raw output by removing ANSI codes, ``\\r`` bars, timestamps,
    and VCS token values.

    This is the main entry point for output sanitization.  It applies
    all cleaners in sequence, mimicking ``CI=true`` / ``--no-color``
    forcing for containers that do not support those flags natively.

    Args:
        text: Raw output from the command.

    Returns:
        Clean, reader-friendly text with secrets masked.
    """
    result = strip_ansi(text)
    result = strip_carriage_returns(result)
    result = strip_timestamps(result)
    result = mask_tokens(result)
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

    Raises:
        ValueError: If *max_lines* is less than 1.
    """
    if max_lines < 1:
        raise ValueError(
            f"max_lines must be a positive integer (>= 1), got {max_lines}"
        )

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



# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------


#: Rough heuristic: 1 token ≈ 4 characters for English text.
#: Used for max_output_tokens budget calculation.
_CHARS_PER_TOKEN: float = 4.0


def estimate_tokens(text: str) -> int:
    """Estimate the number of tokens in *text*.

    Uses a simple heuristic of *chars_per_token* characters per token.
    For English text this is approximately correct; for code/mixed
    content it is a reasonable approximation.

    Args:
        text: The text to estimate.

    Returns:
        Estimated token count (always >= 1 for non-empty text).
    """
    if not text:
        return 0
    return max(1, round(len(text) / _CHARS_PER_TOKEN))


def truncate_by_tokens(
    text: str,
    max_tokens: int,
) -> tuple[str, int]:
    """Truncate *text* to fit within *max_tokens* estimated tokens.

    The truncation keeps the beginning and end of the text, omitting
    the middle portion with a note.  This is useful when a token
    budget is specified via ``max_output_tokens``.

    Args:
        text: The text to truncate.
        max_tokens: Maximum estimated token count allowed.

    Returns:
        Tuple of ``(truncated_text, original_estimated_tokens)``.
    """
    if not text:
        return "", 0

    original_tokens = estimate_tokens(text)
    if original_tokens <= max_tokens:
        return text, original_tokens

    lines = text.split("\n")
    total_lines = len(lines)

    # Reserve tokens for the truncation notice
    notice = f"\n... (truncated to {max_tokens} estimated tokens, originally {original_tokens}) ...\n"
    notice_tokens = estimate_tokens(notice)

    budget = max_tokens - notice_tokens
    if budget <= 0:
        return notice, original_tokens

    # Line-based truncation: keep head and tail proportionally
    max_lines = max(1, total_lines * budget // original_tokens)
    head_count = max_lines // 2
    tail_count = max_lines - head_count

    head = lines[:head_count]
    tail = lines[-tail_count:] if tail_count > 0 else []
    result = "\n".join(head) + notice + "\n".join(tail)
    return result, original_tokens


# ---------------------------------------------------------------------------
# Failure fingerprinting
# ---------------------------------------------------------------------------


#: Patterns to normalize error messages before fingerprinting.
#: Strips line numbers, file paths, and volatile offsets so that
#: semantically identical failures produce the same fingerprint.
#: This is intentionally aggressive — even the filename is removed
#: so that the same error type in different files compresses together
#: (e.g. ``NameError`` in ``foo.py`` and ``bar.py`` share one fingerprint).
_NORMALIZE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r'File "[^"]+", line \d+'), 'File "", line N'),
    (re.compile(r'line \d+'), 'line N'),
    (re.compile(r':\d+:\d+'), ':N:N'),
]


#: Common failure patterns to detect for fingerprinting.
_FAILURE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"ERROR\s+\w+"),
    re.compile(r"FAILED\s+test_\w+"),
    re.compile(r"AssertionError"),
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(r"RuntimeError"),
    re.compile(r"ModuleNotFoundError"),
    re.compile(r"ImportError"),
    re.compile(r"SyntaxError"),
]


def compute_failure_fingerprint(text: str) -> str:
    """Compute a compact fingerprint of failure patterns in *text*.

    Extracts the first occurrence of each failure pattern and hashes
    them together to produce a signature.  Two failures with the same
    fingerprint are considered "isomorphic" (same failure type and
    similar location).

    Normalizes file paths and line numbers before fingerprinting so
    that ``File \"foo.py\", line 42`` and ``File \"bar.py\", line 7``
    that differ only in location produce the same fingerprint.

    Args:
        text: Output text that may contain failure patterns.

    Returns:
        Hex digest fingerprint string, or empty string if no failure
        patterns are found.
    """
    # Normalize volatile parts (line numbers, file paths)
    normalized = text
    for pat, repl in _NORMALIZE_PATTERNS:
        normalized = pat.sub(repl, normalized)

    matches: list[str] = []
    for pattern in _FAILURE_PATTERNS:
        m = pattern.search(normalized)
        if m:
            matches.append(m.group())
    if not matches:
        return ""
    identifier = "\n".join(matches)
    return hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:16]


def compress_failures(text: str) -> str:
    """Compress known failure patterns into ``[×N]`` summary lines.

    Detects repeated isomorphic failures (same fingerprint) and
    replaces them with a compact summary::

        FAILED test_login
        [×5] FAILED test_login  (isomorphic failures compressed)

    Handles both single-line failures and multi-line failure blocks
    (e.g. tracebacks) by computing fingerprints on failure blocks.

    Args:
        text: Output text with potential repeated failures.

    Returns:
        Text with repeated failure patterns compressed.
    """
    lines = text.split("\n")
    if len(lines) < 3:
        return text

    # ---- Phase 1: Build failure blocks ----
    # A failure block is one or more consecutive lines that belong
    # to the same failure instance.  Rules:
    # - Lines with the SAME fingerprint as the block start are new
    #   blocks (repeated single-line failures).
    # - Lines with a DIFFERENT fingerprint extend the current block
    #   (multi-line failures like tracebacks).
    # - Lines without a fingerprint end the current block.
    blocks: list[tuple[int, int, str, str]] = []  # (start, end_excl, fp, text)
    i = 0
    while i < len(lines):
        line = lines[i]
        fp = compute_failure_fingerprint(line)
        if not fp:
            i += 1
            continue

        block_start = i
        block_start_fp = fp
        j = i + 1
        while j < len(lines):
            next_fp = compute_failure_fingerprint(lines[j])
            if not next_fp:
                # Non-failure line ends the block
                break
            if next_fp == block_start_fp:
                # Same fingerprint as block start: new block starting
                break
            # Different fingerprint: extends multi-line block (traceback)
            j += 1
        block_text = "\n".join(lines[block_start:j])
        # Compute fingerprint of the full block text for multi-line matching
        block_fp = compute_failure_fingerprint(block_text) or block_start_fp
        blocks.append((block_start, j, block_fp, block_text))
        i = j

    if not blocks:
        return "\n".join(lines)

    # ---- Phase 2: Compress repeated blocks ----
    # Walk through blocks and compress consecutive blocks with the same
    # fingerprint.
    result: list[tuple[int, int, str]] = []  # (start, end_excl, compressed_text)
    bi = 0
    while bi < len(blocks):
        blk_start, blk_end, blk_fp, blk_text = blocks[bi]

        # Count consecutive blocks with the same fingerprint
        count = 1
        bj = bi + 1
        while bj < len(blocks):
            nxt_start, nxt_end, nxt_fp, nxt_text = blocks[bj]
            if nxt_fp == blk_fp and nxt_text == blk_text:
                count += 1
                bj += 1
            else:
                break

        if count >= 3:
            # Compress: keep only the first line of the block with a summary
            first_line = blk_text.split("\n")[0]
            compressed = f"[×{count}] {first_line}  (isomorphic failures compressed)"
            last_end = blocks[bj - 1][1]
            result.append((blk_start, last_end, compressed))
        else:
            for k in range(bi, bj):
                result.append((blocks[k][0], blocks[k][1], blocks[k][3]))
        bi = bj

    # ---- Phase 3: Rebuild output ----
    # Merge compressed blocks with non-failure lines
    i = 0
    out: list[str] = []
    block_idx = 0
    while i < len(lines):
        if block_idx < len(result) and i == result[block_idx][0]:
            out.append(result[block_idx][2])
            i = result[block_idx][1]
            block_idx += 1
        else:
            out.append(lines[i])
            i += 1

    return "\n".join(out)
