"""Insight metrics for the dashboard (Issue #777).

All functions take the aggregated state from :func:`sunaba.phase.aggregate_run_phases`
and return computed metrics as plain dicts.  No HTTP, no rendering — just numbers.

Each metric is a separate public function so the dashboard can call whichever
it needs.  ``compute_all_insights`` is a convenience wrapper that returns all
five metrics in one dict, optionally filtered to a time window.  The
Issue #783 phase-1 resource observations (``initialize_duration_distribution``
and ``busy_refusal_counts``) are separate public functions of the same shape,
called alongside ``compute_all_insights`` by the /insights page.

The module also accepts an optional *all_tools* set (keys from
``_PHASE_MAP``) so callers can supply the universe of known operations
for the "unused tools" metric without circular imports.
"""

from __future__ import annotations

from typing import Any

from sunaba.phase import _parse_iso


def filter_runs_by_period(
    state: dict[str, dict[str, Any]],
    from_ts: str | None,
    to_ts: str | None,
) -> dict[str, dict[str, Any]]:
    """Return only the runs whose time span overlaps [*from_ts*, *to_ts*).

    A run is included when its ``last_ts`` is >= *from_ts* and its
    ``start_ts`` is < *to_ts* (capped by ``last_ts``).  When both bounds
    are ``None``, all runs are returned.

    Timestamps are compared numerically (Unix seconds via :func:`_parse_iso`)
    so mixed ISO formats (``Z`` vs ``+00:00`` suffixes) cannot misclassify
    entries at the exact cutoff instant.  Runs whose timestamps cannot be
    parsed fall back to lexicographic string comparison.
    """
    if from_ts is None and to_ts is None:
        return state

    f_from = _parse_iso(from_ts) if from_ts is not None else None
    f_to = _parse_iso(to_ts) if to_ts is not None else None

    filtered: dict[str, dict[str, Any]] = {}
    for rid, run in state.items():
        start = run.get("start_ts", "")
        last = run.get("last_ts", "")
        if not last:
            last = start  # single-entry run

        s = _parse_iso(start)
        e = _parse_iso(last)

        # Overlap test: run [start, last] ∩ [from, to) non-empty.
        if f_from is not None:
            if s is not None and e is not None:
                if e < f_from:
                    continue
            elif last < from_ts:
                continue
        if f_to is not None:
            if s is not None and e is not None:
                if s >= f_to:
                    continue
            elif start >= to_ts:
                continue
        filtered[rid] = run
    return filtered


def compute_all_insights(
    state: dict[str, dict[str, Any]],
    *,
    from_ts: str | None = None,
    to_ts: str | None = None,
    all_tools: set[str] | None = None,
) -> dict[str, Any]:
    """Compute all five metrics from *state*, optionally time-filtered.

    Args:
        state: The aggregated state from :func:`aggregate_run_phases`.
        from_ts: Inclusive lower bound (ISO format) for the period filter.
        to_ts: Exclusive upper bound (ISO format) for the period filter.
        all_tools: The universe of known tool/operation keys (e.g. ``tool:write_file``).
                   When omitted the "unused tools" metric returns an empty list.

    Returns:
        A dict with keys ``per_tool_error_rate``, ``first_verify_failure_by_image``,
        ``roundtrip_distribution``, ``unused_tools``, ``run_distributions``.
    """
    runs = filter_runs_by_period(state, from_ts, to_ts)
    return {
        "per_tool_error_rate": per_tool_error_rate(runs),
        "first_verify_failure_by_image": first_verify_failure_by_image(runs),
        "roundtrip_distribution": edit_verify_roundtrip_distribution(runs),
        "unused_tools": unused_tools(runs, all_tools=all_tools),
        "run_distributions": run_duration_op_distribution(runs),
    }


# ──────────────────────────────────────────────────────────────────────
# Metric 1: Per-tool error rate
# ──────────────────────────────────────────────────────────────────────


def per_tool_error_rate(
    state: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Return the failure rate per operation/tool, plus recovery-action
    distribution.

    The failure rate for each operation is ``failures / calls``.  The
    recovery distribution maps each failing operation to a dict of
    follow-up actions and their frequencies.
    """
    totals: dict[str, dict[str, int]] = {}  # op → {"calls": N, "fails": N}
    recovery: dict[str, dict[str, int]] = {}  # failed_op → {next_op: N}

    for run in state.values():
        for op, n in run.get("op_calls", {}).items():
            totals.setdefault(op, {"calls": 0, "fails": 0})["calls"] += n
        for op, n in run.get("op_failures", {}).items():
            totals.setdefault(op, {"calls": 0, "fails": 0})["fails"] += n

        # Merge per-run failure_recovery
        for failed_op, next_ops in run.get("failure_recovery", {}).items():
            rec = recovery.setdefault(failed_op, {})
            for next_op, n in next_ops.items():
                rec[next_op] = rec.get(next_op, 0) + n

    # Build per-tool results
    by_tool: list[dict[str, Any]] = []
    for op in sorted(totals.keys()):
        t = totals[op]
        calls = t["calls"]
        fails = t["fails"]
        rate = round(fails / calls, 4) if calls > 0 else 0.0
        by_tool.append({
            "operation": op,
            "calls": calls,
            "failures": fails,
            "failure_rate": rate,
        })

    return {
        "by_tool": by_tool,
        "total_calls": sum(t["calls"] for t in totals.values()),
        "total_failures": sum(t["fails"] for t in totals.values()),
        "recovery_distribution": recovery,
    }


# ──────────────────────────────────────────────────────────────────────
# Metric 2: First-verify failure rate by image
# ──────────────────────────────────────────────────────────────────────


def first_verify_failure_by_image(
    state: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Return the share of runs whose **first verify** failed, grouped by
    container image.

    The first verify is the first ``verify_outcome`` entry in the run's
    ``verify_timeline`` (see :func:`aggregate_run_phases`).  Runs without
    any verify outcome are excluded from the denominator.
    """
    by_image: dict[str, dict[str, int]] = {}  # image → {"total": N, "failed": N}
    total_runs_with_verify = 0
    total_first_failed = 0

    for run in state.values():
        image = run.get("image", "unknown")
        vt = run.get("verify_timeline", [])

        # Find the first verify_outcome
        first = None
        for entry in vt:
            if entry.get("type") == "verify_outcome":
                first = entry
                break
        if first is None:
            continue  # no verify — skip this run

        info = by_image.setdefault(image, {"total": 0, "failed": 0})
        info["total"] += 1
        total_runs_with_verify += 1
        if not first.get("passed", True):
            info["failed"] += 1
            total_first_failed += 1

    by_image_list: list[dict[str, Any]] = []
    for image in sorted(by_image.keys()):
        info = by_image[image]
        total = info["total"]
        failed = info["failed"]
        rate = round(failed / total, 4) if total > 0 else 0.0
        by_image_list.append({
            "image": image,
            "total_runs": total,
            "first_verify_failed": failed,
            "failure_rate": rate,
        })

    overall_rate = (
        round(total_first_failed / total_runs_with_verify, 4)
        if total_runs_with_verify > 0
        else 0.0
    )

    return {
        "by_image": by_image_list,
        "overall": {
            "total_runs_with_verify": total_runs_with_verify,
            "total_first_failed": total_first_failed,
            "failure_rate": overall_rate,
        },
    }


# ──────────────────────────────────────────────────────────────────────
# Metric 3: edit → verify roundtrip distribution
# ──────────────────────────────────────────────────────────────────────


def edit_verify_roundtrip_distribution(
    state: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Return a histogram of ``edit_verify_roundtrips`` across runs.

    Each run contributes one observation: its roundtrip count (0 or more).
    The histogram buckets are 0, 1, 2, …, 5+.
    """
    buckets: dict[int, int] = {}
    total_runs = 0
    total_roundtrips = 0

    for run in state.values():
        n = run.get("edit_verify_roundtrips", 0)
        total_runs += 1
        total_roundtrips += n
        key = n if n < 6 else 5  # 5 = "5+"
        buckets[key] = buckets.get(key, 0) + 1

    histogram: list[dict[str, Any]] = []
    for k in range(6):
        label = f"{k}" if k < 5 else "5+"
        count = buckets.get(k, 0)
        histogram.append({"bucket": label, "count": count, "value": k if k < 5 else 5})

    return {
        "histogram": histogram,
        "total_runs": total_runs,
        "total_roundtrips": total_roundtrips,
        "mean_roundtrips": (
            round(total_roundtrips / total_runs, 2) if total_runs > 0 else 0.0
        ),
    }


# ──────────────────────────────────────────────────────────────────────
# Metric 4: Unused tools
# ──────────────────────────────────────────────────────────────────────


def unused_tools(
    state: dict[str, dict[str, Any]],
    *,
    all_tools: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return tool/operation keys with **zero calls** in *state*.

    Args:
        state: Aggregated state from :func:`aggregate_run_phases`.
        all_tools: Universe of known operation keys.  When ``None``,
            returns an empty list.

    Returns:
        List of ``{operation, "phase"?, reason}`` dicts, sorted by key.
    """
    if all_tools is None:
        return []

    used: set[str] = set()
    for run in state.values():
        used.update(k for k, n in run.get("op_calls", {}).items() if n > 0)

    result: list[dict[str, Any]] = []
    for tool in sorted(all_tools - used):
        # Skip "exec" / "stop" (always used implicitly) and "busy_refusal"
        # (a resource event, not a tool -- Issue #783).
        if tool in ("exec", "stop", "busy_refusal"):
            continue
        result.append({
            "operation": tool,
            "reason": "zero calls in selected period",
        })
    return result


# ──────────────────────────────────────────────────────────────────────
# Metric 5: Run duration & operation-count distributions
# ──────────────────────────────────────────────────────────────────────


def run_duration_op_distribution(
    state: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Return run duration and operation-count distributions, grouped by
    ``repo`` and by ``session_label``.

    Duration is ``last_ts - start_ts`` in seconds (float).  Operation
    count is the total of ``op_calls`` values for each run.
    """
    # ── by repo ──
    by_repo: dict[str, list[dict[str, Any]]] = {}
    for run in state.values():
        repo = run.get("repo") or "(no repo)"
        dur = _run_duration_s(run)
        ops = sum(run.get("op_calls", {}).values())
        by_repo.setdefault(repo, []).append({"duration_s": dur, "op_count": ops})

    repos: list[dict[str, Any]] = []
    for repo in sorted(by_repo.keys()):
        items = by_repo[repo]
        repos.append(_bin_distribution(repo, items))

    # ── by session_label ──
    by_label: dict[str, list[dict[str, Any]]] = {}
    for run in state.values():
        sl = run.get("session_label") or "(no label)"
        dur = _run_duration_s(run)
        ops = sum(run.get("op_calls", {}).values())
        by_label.setdefault(sl, []).append({"duration_s": dur, "op_count": ops})

    labels: list[dict[str, Any]] = []
    for sl in sorted(by_label.keys()):
        items = by_label[sl]
        labels.append(_bin_distribution(sl, items))

    return {
        "by_repo": repos,
        "by_session_label": labels,
    }


# ── helpers ──────────────────────────────────────────────────────────


def _run_duration_s(run: dict[str, Any]) -> float | None:
    """Return the duration of *run* in seconds, or ``None``."""
    s = _parse_iso(run.get("start_ts"))
    e = _parse_iso(run.get("last_ts"))
    if s is not None and e is not None and e >= s:
        return round(e - s, 1)
    return None


def _bin_distribution(
    key: str,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute summary stats for a list of ``{duration_s, op_count}`` items.

    Returns a dict with ``key``, ``count``, ``duration_stats``, and
    ``op_count_stats``.
    """
    durations = [it["duration_s"] for it in items if it["duration_s"] is not None]
    op_counts = [it["op_count"] for it in items]

    return {
        "key": key,
        "run_count": len(items),
        "duration_stats": _stats(durations),
        "op_count_stats": _stats(op_counts),
    }


def _stats(values: list[float]) -> dict[str, Any]:
    """Summarise *values* as ``{count, min, max, mean, median}``.

    All values are rounded to 1 decimal; an empty list yields all-zero
    stats.  Shared by :func:`_bin_distribution` (metric 5) and the
    Issue #783 initialize-duration metric.
    """
    n = len(values)
    if n == 0:
        return {"count": 0, "min": 0, "max": 0, "mean": 0, "median": 0}
    srt = sorted(values)
    median = srt[n // 2] if n % 2 else (srt[n // 2 - 1] + srt[n // 2]) / 2
    return {
        "count": n,
        "min": srt[0],
        "max": srt[-1],
        "mean": round(sum(values) / n, 1),
        "median": round(median, 1),
    }


# ---------------------------------------------------------------------------
# Metric 6: initialize duration distribution (Issue #783, observation 3)
# ---------------------------------------------------------------------------


def initialize_duration_distribution(
    state: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Return the distribution of ``initialize`` -> ``initialize_complete``
    durations across runs.

    One observation per run that recorded a completion; the duration is
    the span between the ``initialize`` and ``initialize_complete``
    journal entries (captured by the aggregation state as
    ``init_start_ts`` / ``init_complete_ts``).  Runs that recorded an
    ``initialize`` but no completion are split by what the journal
    positively establishes (issue #783 review): a run that also recorded
    a ``stop`` is ``abandoned`` (it ended without ever completing init);
    one that did not is ``in_flight`` (still initializing, or died
    without a stop entry -- the journal cannot distinguish those, so the
    label must not claim abandonment).

    The histogram buckets are fixed second ranges: ``0-5``, ``5-15``,
    ``15-30``, ``30-60``, ``60-120``, ``120-300`` and ``300+``.
    """
    durations: list[float] = []
    abandoned = 0
    in_flight = 0
    for run in state.values():
        s = _parse_iso(run.get("init_start_ts"))
        e = _parse_iso(run.get("init_complete_ts"))
        if s is None or e is None:
            if run.get("init_start_ts") is not None:
                if run.get("stopped"):
                    abandoned += 1
                else:
                    in_flight += 1
            continue
        if e < s:
            continue  # clock skew / malformed timestamps: not an observation
        durations.append(round(e - s, 1))

    buckets: list[tuple[float, float | None]] = [
        (0, 5), (5, 15), (15, 30), (30, 60), (60, 120), (120, 300), (300, None),
    ]
    histogram: list[dict[str, Any]] = []
    for lo, hi in buckets:
        label = f"{int(lo)}-{int(hi)}" if hi is not None else f"{int(lo)}+"
        if hi is None:
            count = sum(1 for d in durations if lo <= d)
        else:
            count = sum(1 for d in durations if lo <= d < hi)
        histogram.append({
            "bucket": label,
            "count": count,
            "lo": lo,
            "hi": hi,
        })

    return {
        "stats": _stats(durations),
        "histogram": histogram,
        "abandoned": abandoned,
        "in_flight": in_flight,
        "total_runs": len(state),
    }


# ---------------------------------------------------------------------------
# Metric 7: per-pool busy-refusal counts (Issue #783, initialize-wait proxy)
# ---------------------------------------------------------------------------


def busy_refusal_counts(state: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Return per-pool concurrency-cap refusal counts (``busy_refusal``
    journal entries, Issue #784).

    In the post-#784 world docker-bound acquisition is non-blocking, so a
    saturated pool *refuses* instead of queueing: per-pool refusal counts
    are the observable proxy for "initialize wait".  The aggregation
    state carries the counts per run (``busy_refusals`` keyed by pool
    name -- ``docker`` / ``recovery``); this metric sums them across
    runs, filtered by the caller's period window like every other
    insight.
    """
    by_pool: dict[str, int] = {}
    total = 0
    for run in state.values():
        for pool, n in run.get("busy_refusals", {}).items():
            by_pool[pool] = by_pool.get(pool, 0) + n
            total += n
    return {
        "by_pool": [{"pool": p, "count": n} for p, n in sorted(by_pool.items())],
        "total": total,
    }
