"""Local web dashboard for observability (§9).

Serves an auto-refreshing HTML dashboard on localhost that shows running
containers, run history, pass/fail counts, resource usage.

``/`` and ``/trace/*`` are read-only observation.  ``/containers`` is a
control plane: it can stop a container (Issue #528).  That one POST is why
this module carries a CSRF token and a Host allowlist -- see
:func:`_check_control_request`.

Uses Python's built-in ``http.server`` — no external dependencies.
"""
from __future__ import annotations

import copy
import json
import secrets
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

# The render helpers moved to ``sunaba.dashboard_render`` are re-exported here:
# tests and callers still reach them as ``sunaba.dashboard.<name>``, so a couple
# are imported for that alone (``# noqa: F401``) rather than used below.
from sunaba.dashboard_render import (
    _build_phase_view,
    _escape,
    _fmt_bytes,
    _fmt_duration,
    _fmt_hhmm,
    _js_string,  # noqa: F401
    _live_script,
    _now_iso,
    _render_bar,
    _render_cd_rate_row,  # noqa: F401
    _render_health_badge,
    _render_insights_page,
    _render_nav,
    _render_sidecars,
    _render_status_cell,
    _render_tool_usage_panel,
    _short_image,
    _strip_error_prefix,
)
from sunaba.dashboard_templates import (
    _CONTAINER_ROW,
    _CONTAINERS_HTML,
    _DASHBOARD_HTML,
    _RUN_ROW,
    _STOP_CONFIRM_HTML,
    _STYLE,
    _TRACE_HTML,
)
from sunaba.health import HEALTH_ORDER, classify_all_runs, classify_run_health
from sunaba.insights import (
    busy_refusal_counts,
    compute_all_insights,
    filter_runs_by_period,
    initialize_duration_distribution,
)
from sunaba.journal import (
    _MAX_JOURNAL_SIZE,
    get_journal_live_size,
    get_journal_path,
    get_runs,
    get_tool_usage,
    read_journal,
    read_journal_snapshot,
    read_journal_tail,
    timeline_from_lifecycle,
)
from sunaba.phase import _PHASE_MAP, aggregate_run_phases, phase_state_for_run
from sunaba.resources import cached_disk_usage
from sunaba.security import KIND_PROXY, KIND_SANDBOX
from sunaba.tools.container import list_managed_containers, sandbox_stop

#: Per-process CSRF token, embedded in every Stop form and required on POST.
#: The same-origin policy keeps a hostile page from reading it out of the
#: dashboard, which is what makes it a defence rather than decoration.
_CSRF_TOKEN: str = secrets.token_urlsafe(32)

#: Hosts the control plane will accept a POST for.  A page on the open web can
#: resolve its own domain to 127.0.0.1 (DNS rebinding) and then talk to this
#: server as same-origin; pinning the Host header shuts that door, since the
#: rebound request still carries the attacker's hostname.
_ALLOWED_CONTROL_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "[::1]"})

# ---------------------------------------------------------------------------
# Incremental aggregation cache (Issue #789)
# ---------------------------------------------------------------------------

#: Server-side cache of everything the per-poll fragments render from the
#: journal, maintained incrementally: every poll reads only the live-file
#: tail via ``read_journal_tail`` and folds the delta through
#: ``aggregate_run_phases`` (the #774 incremental contract
#: ``(state, new_entries) -> state``), instead of re-parsing the whole
#: journal on every 1-second poll.  The derived views the fragments render
#: -- the run summaries (mirroring ``journal.get_runs``), the
#: container->run_id mapping (``journal.get_run_id_per_container``), the
#: active test environments (``journal.get_active_environments``) and the
#: total entry count -- are maintained from the same deltas, so *no*
#: fragment path full-parses.  Only the PARSING is incremental --
#: classification (``classify_all_runs``) still runs per poll on the
#: cached state, because health is time-dependent.
#:
#: Keyed by journal path so each test's isolated journal (and any real path
#: change) gets an independent cache; per-process production use has a
#: single path and a single entry.  The first call for a path primes the
#: cache with one full read (backup file included), so no entries written
#: before priming are skipped; a rotation rebuilds once from a full read
#: and then continues incrementally.
#:
#: Thread safety: polls arrive concurrently on ThreadingHTTPServer threads,
#: so all cache mutation happens under ``_journal_agg_lock``.  The stored
#: offset advances only inside the lock, which makes delta application
#: atomic -- the same entries are never applied twice (that would corrupt
#: counters like ``op_calls``) and never torn.  Callers receive a deep
#: snapshot taken under the lock: ``aggregate_run_phases`` folds later
#: deltas into the same per-run dicts in place, and a render iterating a
#: counter dict mid-update could raise on a resize -- a crash is not the
#: acceptable "stale-but-consistent read".  Each caller therefore gets a
#: point-in-time copy (the state is far smaller than the journal it
#: summarises), and the next poll self-corrects freshness.
_journal_agg_lock: threading.Lock = threading.Lock()
_journal_agg_cache: dict[str, dict[str, Any]] = {}


def _apply_delta(cached: dict[str, Any], entries: list[dict[str, Any]]) -> None:
    """Fold a journal delta into every part of a cache entry, in place.

    Caller must hold ``_journal_agg_lock``.  Updates the aggregation state
    and the derived views (entry count, container->run_id mapping, active
    test environments) from the same delta, so all of them stay consistent
    at the same journal offset.
    """
    cached["entry_count"] += len(entries)
    run_ids = cached["run_ids"]
    active_envs = cached["active_envs"]
    lifecycle = cached["container_lifecycle"]
    for entry in entries:
        cid = entry.get("container_id")
        if cid:
            if entry.get("operation") == "stop":
                run_ids.pop(cid, None)
            else:
                rid = entry.get("run_id")
                if rid:
                    run_ids[cid] = rid
            if entry.get("operation") == "test_environment":
                if entry.get("environment_status") == "stopped":
                    active_envs.pop(cid, None)
                else:
                    active_envs[cid] = entry
            # Issue #783 observation 1: maintain the container-lifecycle map
            # (initialize opens / stop closes a lifetime) incrementally,
            # mirroring journal.container_concurrency_timeline.  The timeline
            # is derived from this map via journal.timeline_from_lifecycle.
            op = entry.get("operation")
            if op == "initialize":
                lifecycle[cid] = {"init_ts": entry.get("ts"), "stop_ts": None}
            elif op == "stop":
                lc = lifecycle.setdefault(cid, {"init_ts": None, "stop_ts": None})
                lc["stop_ts"] = entry.get("ts")
    cached["state"] = aggregate_run_phases(cached["state"], entries)


def _cached_entry() -> dict[str, Any]:
    """Return the cache entry for the live journal path, priming it with a
    full read (backup + live) when this is the first call.

    Caller must hold ``_journal_agg_lock``.  Priming captures the live
    file's generation so later rotations are detected by identity, not just
    by size; an entry appended between the snapshot and the capture is not
    applied here -- the stored offset stays at the snapshot's value, so the
    next poll re-reads it exactly once.
    """
    path = get_journal_path()
    cached = _journal_agg_cache.get(path)
    if cached is None:
        entries, offset = read_journal_snapshot()
        cached = {
            "offset": offset,
            "generation": None,
            "state": None,
            "entry_count": 0,
            "run_ids": {},
            "active_envs": {},
            # Issue #783: container_id -> {"init_ts", "stop_ts"} lifecycle
            # map maintained from journal deltas (see _apply_delta).
            "container_lifecycle": {},
        }
        _journal_agg_cache[path] = cached
        _apply_delta(cached, entries)
        cached["generation"] = read_journal_tail(offset, None)[3]
    return cached


def _cached_agg_state() -> dict[str, dict[str, Any]]:
    """Return a point-in-time snapshot of the aggregated run-phase state.

    The first call per journal path performs one full read (``journal.log.1``
    included) to prime the cache; every later call reads only the live-file
    tail and folds the delta through :func:`sunaba.phase.aggregate_run_phases`.
    After a rotation the cache rebuilds once from a full read, then
    continues incrementally.

    The returned state is a deep copy taken under the cache lock: callers
    may iterate it freely (dict mutation during iteration is a crash risk on
    the live state) and may not corrupt the cache by mutating it.
    """
    with _journal_agg_lock:
        cached = _cached_entry()
        entries, next_offset, rotated, gen = read_journal_tail(
            cached["offset"], cached["generation"]
        )
        if rotated:
            # The live file was replaced: our byte offset no longer
            # identifies history and the backup now holds entries we have
            # never seen.  Rebuild once from a full snapshot (backup
            # included), then continue incrementally.
            entries, offset = read_journal_snapshot()
            cached["offset"] = offset
            cached["entry_count"] = 0
            cached["run_ids"] = {}
            cached["active_envs"] = {}
            cached["container_lifecycle"] = {}
            cached["state"] = None
            _apply_delta(cached, entries)
            cached["generation"] = read_journal_tail(offset, None)[3]
        else:
            _apply_delta(cached, entries)
            cached["offset"] = next_offset
            cached["generation"] = gen
        # ``_apply_delta`` always folds into a real state by this point.
        state: dict[str, dict[str, Any]] | None = cached["state"]
        assert state is not None
        return copy.deepcopy(state)


def _cached_run_ids() -> dict[str, str]:
    """``container_id -> run_id`` at the cache's current journal offset.

    Mirrors :func:`sunaba.journal.get_run_id_per_container` but is
    maintained incrementally from the tail deltas (Issue #789).  Fragments
    call :func:`_cached_agg_state` first so this reflects the same offset
    as the aggregation state they render.
    """
    with _journal_agg_lock:
        cached = _cached_entry()
        return dict(cached["run_ids"])


def _cached_active_envs() -> list[dict[str, Any]]:
    """Active test environments at the cache's current journal offset.

    Mirrors :func:`sunaba.journal.get_active_environments` (Issue #789).
    """
    with _journal_agg_lock:
        cached = _cached_entry()
        return list(cached["active_envs"].values())


def _cached_journal_entry_count() -> int:
    """Total journal entries at the cache's current journal offset.

    The dashboard's journal-card stat; maintained incrementally instead of
    scanning the whole file on every poll (Issue #789).
    """
    with _journal_agg_lock:
        cached = _cached_entry()
        return cached["entry_count"]


def _cached_concurrency_timeline() -> dict[str, Any]:
    """Concurrent-container timeline at the cache's current journal offset.

    Derives ``{current, peak, series}`` from the incrementally maintained
    container-lifecycle map (Issue #783 observation 1) via the shared
    pure function :func:`sunaba.journal.timeline_from_lifecycle` -- the
    same computation the journal full-scan uses, so the two can never
    drift apart.
    """
    with _journal_agg_lock:
        cached = _cached_entry()
        return timeline_from_lifecycle(cached["container_lifecycle"])


#: Idle time past which a container is highlighted as probably forgotten.
#: Three hours is well beyond a working session's natural pauses, and well
#: short of the 21-hour strays that motivated Issue #527.
_STALE_IDLE_SECONDS: float = 3 * 3600


def _render_container_row(
    c: dict[str, Any],
    run_ids: dict[str, str],
    envs: dict[str, dict[str, Any]],
    health_map: dict[str, str] | None = None,
) -> str:
    cid = c.get("container_id", "")
    name = c.get("name")
    allow_network = c.get("allow_network")
    if allow_network is None:
        net, net_cls = "?", "net-unknown"
    elif allow_network:
        net, net_cls = "on", "net-on"
    else:
        net, net_cls = "off", "net-off"

    idle_seconds = c.get("idle_seconds")
    idle_cls = (
        "stale"
        if idle_seconds is not None and idle_seconds >= _STALE_IDLE_SECONDS
        else "dim"
    )

    # Journal and label timestamps are UTC ISO-8601; the column header says so,
    # which buys room to drop the offset suffix.
    created = (c.get("created_at") or "").replace("T", " ").replace("+00:00", "")
    run_id = run_ids.get(cid)
    run_cell = (
        f'<a href="/trace/{_escape(run_id)}" style="color:#58a6ff">{_escape(run_id)}</a>'
        if run_id
        else '<span class="dim">—</span>'
    )
    if run_id:
        health = (health_map or {}).get(run_id)
        if health:
            run_cell = f'{_render_health_badge(health)} {run_cell}'

    return _CONTAINER_ROW.format(
        name=_escape(name) if name else '<span class="dim">(unnamed)</span>',
        cid=_escape(cid),
        image=_escape(_short_image(str(c.get("image", "")))),
        status_cell=_render_status_cell(str(c.get("status", "")), envs.get(cid)),
        net=net,
        net_cls=net_cls,
        created=_escape(created) if created else '<span class="dim">—</span>',
        idle=_fmt_duration(idle_seconds),
        idle_cls=idle_cls,
        run=run_cell,
        csrf=_escape(_CSRF_TOKEN),
    )


def _host_allowed(hostname: str) -> bool:
    """Whether a control-plane POST claiming *hostname* may be trusted.

    The Host pin exists to defeat DNS rebinding, which is only a threat while
    the dashboard sits on an address the open web cannot route to.  An operator
    who passed ``--dashboard-host`` a non-loopback address has deliberately
    published it and may reach it under any hostname, so pinning there would
    only break their Stop button; the CSRF token still guards them.
    """
    if _dashboard_host not in _ALLOWED_CONTROL_HOSTS:
        return True
    return hostname in _ALLOWED_CONTROL_HOSTS


def _check_csrf(token: str) -> str | None:
    """Return a refusal reason for an invalid CSRF token, or ``None`` to allow.

    Factorised out of ``_DashboardHandler._check_control_request`` so the
    CSRF guard survives future handler/adaptor swaps.
    """
    if not secrets.compare_digest(token, _CSRF_TOKEN):
        return "CSRF token mismatch"
    return None


def _check_host_header(host_header: str) -> str | None:
    """Return a refusal reason for a disallowed Host header, or ``None``.

    Factorised out of ``_DashboardHandler._check_control_request`` so the
    Host-allowlist guard survives future handler/adaptor swaps.
    """
    hostname = (
        host_header.split("]")[0] + "]"
        if host_header.startswith("[")
        else host_header.split(":")[0]
    )
    if not _host_allowed(hostname):
        return "control plane is loopback-only"
    return None


def _render_stop_confirm(container_id: str, warning: str) -> str:
    """Render the confirmation for a container with unpushed work (Issue #528).

    The warning is ``sandbox_stop``'s own, passed through rather than replaced
    by a generic prompt: what makes a confirmation worth reading is that it
    tells you something you did not already know.
    """
    # "Use force=True to override" is the tool telling an LLM about its own
    # parameter; on this page the override is the button underneath.  The part
    # that matters -- how much unpushed work is at stake -- is kept verbatim.
    detail = _strip_error_prefix(warning).replace(
        "Use force=True to override.", ""
    ).strip()
    return _STOP_CONFIRM_HTML.format(
        style=_STYLE,
        nav=_render_nav("containers"),
        cid=_escape(container_id),
        warning=_escape(detail),
        csrf=_escape(_CSRF_TOKEN),
    )


def _containers_fragments(now: str | None = None) -> dict[str, str]:
    """Server-rendered fragments of the ``/containers`` page (Issue #776).

    Shared by the initial render and the live-update poll, so a polled
    fragment is byte-identical to what a reload would have rendered.  The
    row data (status, health badges, run list) is Docker + journal state, so
    it is re-rendered on every poll even when the journal delta is empty: a
    container can die without writing a journal entry.

    Returns ``rows_html`` (the ``<tbody>`` content), ``sidecars_html`` and
    ``error_html`` (the listing-error banner; empty string when all is well).
    """
    containers, error = list_managed_containers()

    # Advance the incremental cache once so the derived views below (run
    # ids, environments, health) all reflect the same journal offset; the
    # cache is fed only from the tail, never a full parse (Issue #789).
    agg_state = _cached_agg_state()
    run_ids = _cached_run_ids()
    envs = {
        env.get("container_id", ""): env
        for env in _cached_active_envs()
    }

    # Per-run health badges (Issue #775): aggregated from the journal, keyed
    # by run_id, so each row can badge the run its container is attached to.
    # Classification stays per-poll because health is time-dependent.
    health_map = classify_all_runs(
        agg_state,
        now if now is not None else _now_iso(),
    )

    sandboxes = [c for c in containers if c.get("kind") != KIND_PROXY]
    sidecars = [c for c in containers if c.get("kind") == KIND_PROXY]

    # Most idle first: the containers someone forgot to stop are the whole
    # reason this page exists, so they sort to the top.
    sandboxes.sort(key=lambda c: c.get("idle_seconds") or 0.0, reverse=True)

    rows = "\n".join(
        _render_container_row(c, run_ids, envs, health_map) for c in sandboxes
    )
    if not rows:
        rows = '<tr><td colspan="8" class="empty">No managed containers</td></tr>'

    error_html = ""
    if error is not None:
        error_html = (
            f'<div class="card" style="margin-bottom:16px">'
            f'<span class="badge err">error</span> '
            f'{_escape(_strip_error_prefix(error))}'
            f'</div>'
        )

    return {
        "rows_html": rows,
        "sidecars_html": _render_sidecars(sidecars),
        "error_html": error_html,
    }


def _render_containers_page(
    failed_stop: str | None = None,
    now: str | None = None,
) -> str:
    """Render the ``/containers`` page from Docker's own view of the world.

    *failed_stop* is a stop that could not be carried out.  It renders in its
    own banner (``#stop-error``), separate from the Docker listing-error
    banner (``#containers-error``): the live-update poll re-renders the
    listing banner every cycle (#776), and a shared element would wipe the
    stop failure one poll after the page loads.  Both banners strip the
    ``Error:`` prefix -- they are never used for anything that isn't a
    failure.

    *now* is the classification timestamp (ISO-8601); defaults to the real
    clock so callers can inject a fixed value for deterministic tests.
    """
    frag = _containers_fragments(now=now)

    stop_error_html = ""
    if failed_stop is not None:
        stop_error_html = (
            f'<div class="card" style="margin-bottom:16px">'
            f'<span class="badge err">error</span> '
            f'{_escape(_strip_error_prefix(failed_stop))}'
            f'</div>'
        )

    return _CONTAINERS_HTML.format(
        style=_STYLE,
        nav=_render_nav("containers"),
        stop_error=stop_error_html,
        error=frag["error_html"],
        rows=frag["rows_html"],
        sidecars=frag["sidecars_html"],
        live_script=_live_script(
            view="containers",
            journal_offset=get_journal_live_size(),
        ),
    )


def _dashboard_fragments() -> dict[str, str]:
    """Server-rendered fragments of the ``/`` page (Issue #776).

    Shared by the initial render and the live-update poll, so a polled
    fragment is byte-identical to what a reload would have rendered.

    Returns the ``stats_card`` and ``journal_card`` inner content plus the
    ``run_rows`` ``<tbody>`` content.
    """
    # The aggregation state and every derived stat below come from the
    # incremental cache (Issue #789): only the journal tail is parsed per
    # poll.  Run summaries are built from the state (mirroring get_runs)
    # rather than by full-parsing the journal on every poll.
    agg_state = _cached_agg_state()
    runs = _run_summaries_from_state(agg_state)
    total_ops = 0
    boundary_count = 0
    vcs_ops = 0
    for r in runs:
        total_ops += r.get("operations", 0)
        boundary_count += r.get("boundary_crossings", 0)
        vcs_ops += r.get("vcs_operations", 0)

    journal_entries = _cached_journal_entry_count()
    jp = get_journal_path()

    active_envs = _cached_active_envs()
    running_services = sum(
        len(env.get("services", [])) for env in active_envs
    )

    # Issue #783 observation 1: concurrent-container timeline from the
    # incrementally maintained lifecycle map (current / peak / trend).
    #
    # The journal is the *trend* source and is labelled as such: it only
    # sees initialize/stop entries, so a container removed without a
    # journaled stop (VM reboot, external docker rm) never decrements the
    # count, and a re-initialized container's earlier lifetime is not part
    # of the peak.  The ground truth for "right now" is the live Docker
    # listing (reconciled against the same managed-container view as the
    # /containers page), shown separately so the headline cannot be
    # silently inflated by journal blind spots.
    timeline = _cached_concurrency_timeline()
    series = timeline.get("series", [])
    peak = timeline.get("peak", 0)
    current = timeline.get("current", 0)
    try:
        live_containers, live_error = list_managed_containers()
        if live_error is not None:
            live_now = None  # docker daemon unreachable: degrade to a dash
        else:
            live_now = sum(1 for c in live_containers if c.get("kind") != KIND_PROXY)
    except Exception:
        live_now = None  # docker client unavailable: degrade to a dash
        live_error = "docker unavailable"
    live_val = str(live_now) if live_now is not None else "\u2014"
    live_note = (
        f'<div class="dim" style="font-size:10px">{_escape(live_error or "")}</div>'
        if live_now is None
        else ""
    )
    # Show the trend as a compact bar list -- last 20 events, oldest left.
    shown = series[-20:]
    max_count = max((max(s["count"] for s in shown), 1)) if shown else 1
    bar_parts: list[str] = []
    for s in shown:
        bar_parts.append(
            _render_bar(s["count"], max_count, _fmt_hhmm(s.get("ts", "")), color="#58a6ff")
        )
    trend_html = (
        '<div class="section-header">Trend (initialize/stop events, UTC)</div>'
        + "".join(bar_parts)
        if shown
        else '<div class="empty">No container lifecycle events recorded</div>'
    )
    concurrency_card = (
        f'<div class="meta">Current (journal)</div>'
        f'<div class="val">{current}</div>'
        f'<div class="meta" style="margin-top:8px">Live now (docker)</div>'
        f'<div class="val">{live_val}</div>'
        f'{live_note}'
        f'<div class="meta" style="margin-top:8px">Peak (journal)</div>'
        f'<div class="val">{peak}</div>'
        f'<div style="margin-top:8px">{trend_html}</div>'
        f'<div class="metric-note" style="margin-top:6px">'
        f'Journal timeline: containers removed without a journaled stop still '
        f'count; a re-initialized container\'s earlier lifetime is not part of '
        f'the peak'
        f'</div>'
    )

    # Issue #783 observation 2: disk usage.  Rendered from the interval
    # cache -- a page render (or 1.5s live poll) never triggers a host
    # probe, it only re-reads the cached measurement (resources.py).
    disk = cached_disk_usage()
    docker_part = disk.get("docker", {})
    jdir = disk.get("journal_dir", {})
    measured_at = disk.get("measured_at", "")
    disk_card = (
        f'<div class="meta">Docker images</div>'
        f'<div class="val">{_fmt_bytes(docker_part.get("images_bytes"))}</div>'
        f'<div class="meta" style="margin-top:8px">Container layers (writable)</div>'
        f'<div class="val">{_fmt_bytes(docker_part.get("containers_bytes"))}</div>'
        f'<div class="meta" style="margin-top:8px">Journal / trace dir</div>'
        f'<div class="val">{_fmt_bytes(jdir.get("bytes"))}</div>'
        f'<div class="meta" style="margin-top:8px">Total (observed components)</div>'
        f'<div class="val">{_fmt_bytes(disk.get("total_bytes"))}</div>'
        f'<div class="meta" style="margin-top:8px">Measured {_escape(_fmt_hhmm(measured_at))} UTC '
        f'<span class="dim">(probe interval 60s)</span></div>'
        f'<div class="mono dim" style="margin-top:4px">{_escape(jdir.get("path", ""))}</div>'
    )

    # Per-run health badges (Issue #775): one classification pass over
    # the whole aggregation state, then a lookup per row.  Classification
    # stays per-poll because health is time-dependent.
    health_map = classify_all_runs(agg_state, _now_iso())

    run_rows_parts: list[str] = []
    for r in runs[:20]:  # show last 20 runs
        status = r.get("status", "running")
        if status == "host":  # container-less run (#778), no lifecycle
            status_cls = "boundary"
        else:
            status_cls = "err" if status == "running" else "ok"
        image_short = _short_image(r.get("image", "unknown"))
        run_rows_parts.append(_RUN_ROW.format(
            run_id=r["run_id"],
            health=_render_health_badge(health_map.get(r["run_id"], "progressing")),
            started=r.get("started", ""),
            image=_escape(image_short),
            ops=r.get("operations", 0),
            crossings=r.get("boundary_crossings", 0),
            status=status,
            status_cls=status_cls,
        ))

    return {
        "stats_card": (
            f'<div class="meta">Total Runs</div>'
            f'<div class="val">{len(runs)}</div>'
            f'<div class="meta" style="margin-top:8px">Total Operations</div>'
            f'<div class="val">{total_ops}</div>'
            f'<div class="meta" style="margin-top:8px">Boundary Crossings</div>'
            f'<div class="val">{boundary_count}</div>'
            f'<div class="meta" style="margin-top:8px">VCS Operations</div>'
            f'<div class="val">{vcs_ops}</div>'
            f'<div class="meta" style="margin-top:8px">Running Services</div>'
            f'<div class="val">{running_services}</div>'
        ),
        "journal_card": (
            f'<div class="meta">Path</div>'
            f'<div class="mono">{_escape(jp)}</div>'
            f'<div class="meta" style="margin-top:8px">Entries</div>'
            f'<div class="val">{journal_entries}</div>'
        ),
        "concurrency_card": concurrency_card,
        "disk_card": disk_card,
        "run_rows": (
            "\n".join(run_rows_parts)
            if run_rows_parts
            else '<tr><td colspan="8" class="empty">No runs recorded</td></tr>'
        ),
    }


def _run_summaries_from_state(
    state: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build the dashboard's run summaries from the aggregation state.

    Replaces ``journal.get_runs`` in the per-poll fragments (Issue #789):
    the journal function full-parses on every poll, while the aggregation
    state carries the same per-run counters incrementally.  The summary
    shape mirrors ``get_runs`` (minus ``session_labels``, which the
    dashboard rows do not render), including its sort order and its
    ``running`` / ``stopped`` / ``host`` status semantics.
    """
    summaries: list[dict[str, Any]] = []
    for rid, run in state.items():
        if run.get("host"):
            status = "host"
        elif run.get("stopped"):
            status = "stopped"
        else:
            status = "running"
        summaries.append({
            "run_id": rid,
            "started": run.get("start_ts") or "",
            "image": run.get("image", "unknown"),
            "operations": run.get("entry_count", 0),
            "boundary_crossings": run.get("boundary_crossings", 0),
            "vcs_operations": run.get("vcs_operations", 0),
            "status": status,
        })
    summaries.sort(key=lambda r: r.get("started", ""), reverse=True)
    return summaries


def _trace_fragments(run_id: str) -> dict[str, str]:
    """Server-rendered header fragments for the trace page (Issue #776).

    Recomputed per poll from the same aggregation the page itself uses (the
    incremental cache, Issue #789), so the phase view and health badge always
    reflect the complete history -- backup file included after a rotation.
    Classification stays per-poll because health is time-dependent; only the
    journal PARSING is incremental.

    Returns ``phase_view`` (escaped HTML), ``health`` (plain label) and
    ``health_cls``.
    """
    agg_state = _cached_agg_state()
    run_state = phase_state_for_run(agg_state, run_id)
    health = classify_run_health(agg_state, run_id, _now_iso())
    health_cls = f"health-{health}" if health in HEALTH_ORDER else "health-progressing"
    return {
        "phase_view": _build_phase_view(run_state),
        "health": health,
        "health_cls": health_cls,
    }


class _DashboardHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the dashboard."""

    def log_message(self, format: str, *args: Any) -> None:
        pass  # suppress access logs

    def _send_html(self, content: str, code: int = 200) -> None:
        data = content.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, data: Any, code: int = 200) -> None:
        content = json.dumps(data, ensure_ascii=False)
        body = content.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]

        if path == "/":
            self._serve_dashboard()
        elif path == "/containers":
            self._send_html(_render_containers_page())
        elif path == "/api/runs":
            self._serve_api_runs()
        elif path == "/api/journal":
            self._serve_api_journal()
        elif path == "/api/tool-usage":
            self._serve_api_tool_usage()
        elif path == "/insights":
            self._serve_insights()
        elif path.startswith("/trace/"):
            self._serve_trace(path)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        if path != "/containers/stop":
            self.send_error(404)
            return

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8") if length else ""
        fields = parse_qs(body)

        denied = self._check_control_request(fields)
        if denied is not None:
            self.send_error(403, denied)
            return

        container_id = (fields.get("container_id") or [""])[0]
        if not container_id:
            self.send_error(400, "container_id required")
            return

        force = (fields.get("force") or [""])[0] == "true"
        self._stop_container(container_id, force=force)

    def _check_control_request(self, fields: dict[str, list[str]]) -> str | None:
        """Return a refusal reason for an untrusted POST, or ``None`` to allow.

        Two gates, both cheap — delegated to module-level functions so the
        protection survives future handler/adaptor swaps:

        * **Host** :func:`_check_host_header`
        * **CSRF token** :func:`_check_csrf`
        """
        host = self.headers.get("Host", "")
        denied = _check_host_header(host)
        if denied is not None:
            return denied
        token = (fields.get("csrf") or [""])[0]
        return _check_csrf(token)

    def _stop_container(self, container_id: str, *, force: bool) -> None:
        # Allowlist, not denylist: this endpoint may stop a container only
        # while Docker still lists it as a sandbox.  The list is a snapshot and
        # the world can move under it, so the question is which way the race
        # should fail -- and "refuse a stop the user can retry" beats "kill the
        # egress proxy every sandbox is sharing" (which restart_policy would
        # bring back anyway, having cut every live container's network on the
        # way).  A denylist fails the other way for any id the snapshot missed.
        containers, _ = list_managed_containers()
        kinds = {c.get("container_id"): c.get("kind") for c in containers}
        if kinds.get(container_id) != KIND_SANDBOX:
            self.send_error(400, f"{container_id} is not a listed sandbox container")
            return

        result = sandbox_stop(container_id, force=force)

        if result.startswith("Error:") and "unpushed checkpoint" in result:
            self._send_html(_render_stop_confirm(container_id, result))
            return
        if result.startswith("Error:"):
            self._send_html(_render_containers_page(failed_stop=result), code=500)
            return

        # POST/redirect/GET: a browser refresh must not re-fire the stop.
        self._send_redirect("/containers")

    def _send_redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve_api_tool_usage(self) -> None:
        qs = ""
        if "?" in self.path:
            qs = self.path.split("?", 1)[1]
        params = {}
        for pair in qs.split("&"):
            if "=" in pair:
                key, val = pair.split("=", 1)
                params[key] = unquote(val)
        from_date = params.get("from")
        to_date = params.get("to")
        usage = get_tool_usage(from_date=from_date, to_date=to_date)
        self._send_json(usage)

    def _serve_dashboard(self) -> None:
        # Parse tool usage time range from query string
        tool_from: str | None = None
        tool_to: str | None = None
        if "?" in self.path:
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            tool_from = qs.get("tool_from", [None])[0]
            tool_to = qs.get("tool_to", [None])[0]

        frag = _dashboard_fragments()

        html_content = _DASHBOARD_HTML.format(
            style=_STYLE,
            nav=_render_nav("home"),
            stats_card=frag["stats_card"],
            journal_card=frag["journal_card"],
            concurrency_card=frag["concurrency_card"],
            disk_card=frag["disk_card"],
            run_rows=frag["run_rows"],
            tool_usage_panel=_render_tool_usage_panel(tool_from, tool_to),
            live_script=_live_script(
                view="dashboard",
                journal_offset=get_journal_live_size(),
            ),
        )
        self._send_html(html_content)

    def _serve_insights(self) -> None:
        """Serve the /insights page with cross-run friction metrics (#777)."""
        # Parse period filter from query string
        period: int | None = None  # days; None = all time
        if "?" in self.path:
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            period_raw = qs.get("period", [None])[0]
            if period_raw and period_raw.isdigit():
                period = int(period_raw)

        agg_state = _cached_agg_state()

        # Compute time bounds for the period filter
        from_ts: str | None = None
        if period is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=period)
            from_ts = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

        insights = compute_all_insights(
            agg_state,
            from_ts=from_ts,
            all_tools=set(_PHASE_MAP.keys()),
        )
        # Issue #783 phase-1 resource observations (observation 3): separate
        # public metric functions of the same shape, computed on the same
        # period-filtered runs as the five core metrics.  Not part of
        # compute_all_insights, whose key set is a fixed contract (#777).
        filtered = filter_runs_by_period(agg_state, from_ts, None)
        insights["initialize_duration_distribution"] = initialize_duration_distribution(filtered)
        insights["busy_refusal_counts"] = busy_refusal_counts(filtered)

        html_content = _render_insights_page(insights, period)
        self._send_html(html_content)

    def _serve_api_runs(self) -> None:
        runs = get_runs()
        self._send_json(runs)

    def _serve_api_journal(self) -> None:
        """Serve journal data; with an ``offset`` query, the diff endpoint.

        Two shapes share this path (Issue #776):

        * ``/api/journal`` (no ``offset``) -- the pre-#776 shape, kept
          byte-compatible: a plain JSON array of the most recent entries.
        * ``/api/journal?offset=N[&gen=G]`` -- the live-update endpoint: a
          JSON object ``{"entries": [...], "next_offset": N', "rotated":
          bool, "generation": G'}`` where *entries* are the complete
          JSON-lines written since byte position N in the live
          ``journal.log`` and *next_offset* is where the caller should poll
          next.  A partial trailing line is never returned.  ``rotated`` is
          ``True`` when the live file was replaced -- detected by the file
          being *smaller* than N, or by its identity token ``generation``
          differing from the echoed ``gen`` (which also catches a
          replacement file that already grew past N); the caller must then
          reset to 0, adopt the new ``generation`` and fully re-draw.

        Optional view parameters (used only by the live-update pages, so the
        base shape above stays thin):

        * ``view=dashboard`` -- adds the ``/`` page fragments (stats card,
          journal card, run rows) on every poll.
        * ``view=containers`` -- adds the ``/containers`` row fragments on
          every poll (Docker state can change without journal growth).
        * ``view=trace&run_id=R`` -- adds the trace header fragments (phase
          view, health badge) on every poll.

        Fragments are re-rendered on every poll, not only when the delta is
        non-empty, because health classification is time-dependent: a run
        that stopped writing crosses the stall threshold with no journal
        growth at all, and the badge must flip without a reload.

        All fragments are rendered by the same view-layer functions the
        pages themselves use.  The endpoint is read-only but still applies
        the Host-header gate to both shapes: a DNS-rebinding page could
        otherwise read journal contents through it.
        """
        qs = ""
        if "?" in self.path:
            qs = self.path.split("?", 1)[1]
        params: dict[str, str] = {}
        for pair in qs.split("&"):
            if "=" in pair:
                key, val = pair.split("=", 1)
                params[key] = unquote(val)

        denied = _check_host_header(self.headers.get("Host", ""))
        if denied is not None:
            self.send_error(403, denied)
            return

        if "offset" not in params:
            self._send_json(read_journal(max_entries=500))
            return

        try:
            offset = int(params["offset"])
        except ValueError:
            self.send_error(400, "offset must be an integer")
            return
        if offset < 0:
            self.send_error(400, "offset must be >= 0")
            return

        # Bound the offset: a legitimate client offset is at most the file
        # size it polled at (itself capped by the rotation ceiling), so any
        # offset beyond the current size plus the ceiling plus a one-line
        # margin is a hand-crafted value that ``seek`` could not represent
        # (OverflowError) -- refuse it instead of dropping the connection.
        # Offsets merely above the current size (rotation detection) are
        # untouched.
        live_size = get_journal_live_size()
        if offset > live_size + _MAX_JOURNAL_SIZE + (1 << 20):
            self.send_error(400, "offset out of range")
            return

        generation: int | None = None
        if "gen" in params:
            try:
                generation = int(params["gen"])
            except ValueError:
                self.send_error(400, "gen must be an integer")
                return

        entries, next_offset, rotated, gen = read_journal_tail(offset, generation)

        payload: dict[str, Any] = {
            "entries": entries,
            "next_offset": next_offset,
            "rotated": rotated,
            "generation": gen,
        }

        view = params.get("view", "")
        if view == "containers":
            # Rows are Docker + journal state; re-render every poll so an
            # externally-killed container shows up without waiting for the
            # next journal entry.
            payload.update(_containers_fragments())
        elif view == "dashboard":
            payload.update(_dashboard_fragments())
        elif view == "trace":
            run_id = params.get("run_id")
            if run_id:
                payload.update(_trace_fragments(run_id))

        self._send_json(payload)

    def _serve_trace(self, path: str) -> None:
        parts = path.split("/")
        if len(parts) < 3:
            self.send_error(400)
            return
        run_id = parts[2].split("?")[0]

        # Check for JSON format request
        fmt = "html"
        if "?" in self.path:
            qs = self.path.split("?", 1)[1]
            for param in qs.split("&"):
                if param == "fmt=json":
                    fmt = "json"
                    break

        # Single snapshot (#776): rows and the poll start offset come from
        # one locked read, so the poll range exactly continues the rendered
        # rows -- an entry appended between the render and the first poll is
        # delivered by the poller, and nothing already rendered is
        # re-delivered (the offset is embedded in the page as the poller's
        # starting position).
        entries, journal_offset = read_journal_snapshot(run_id=run_id)
        if not entries:
            # The real journal has nothing for this run, but the page's
            # read_journal view may still (callers that supply a journal
            # view through it): fall back before declaring the run missing.
            entries = read_journal(run_id=run_id)
            journal_offset = get_journal_live_size()
        if not entries:
            self.send_error(404, "Run not found")
            return

        if fmt == "json":
            trace = {
                "run_id": run_id,
                "started": entries[0].get("ts") if entries else "",
                "ended": entries[-1].get("ts") if entries else "",
                "total_operations": len(entries),
                "boundary_crossings": sum(
                    1 for e in entries
                    if e.get("boundary_crossing") or e.get("operation") == "boundary_crossing"
                ),
                "entries": entries,
            }
            self._send_json(trace)
            return

        # HTML trace
        rows_parts: list[str] = []
        for e in entries:
            op = e.get("operation", "unknown")
            cls = op
            details = ""

            if op == "initialize":
                details = f'image={_escape(e.get("image", ""))} net={e.get("allow_network","")}'
            elif op == "exec":
                cmds = " && ".join(e.get("commands", []))
                if "exit_code" in e:
                    ec = e.get("exit_code", 0)
                    ec_cls = "exit-ok" if ec == 0 else "exit-err"
                    exit_part = f' <span class="{ec_cls}">exit={ec}</span>'
                else:
                    # Exec start entry (Issue #789): recorded before running,
                    # no outcome yet -- label it running, not exit=0.
                    exit_part = ' <span class="exit-err">running</span>'
                details = f'<span class="cmds">{_escape(cmds)}</span>{exit_part}'
            elif op == "boundary_crossing":
                sub_op = e.get("sub_operation", "")
                detail_text = e.get("details", "")
                if sub_op == "issue_view":
                    details = f'<span style="color:#a5d6ff">issue_view</span> {_escape(detail_text)}'
                elif sub_op == "publish":
                    formatted = _escape(detail_text)
                    for word in detail_text.split():
                        idx = word.find("https://github.com/")
                        if idx != -1:
                            url = word[idx:]
                            escaped_url = _escape(url)
                            formatted = formatted.replace(
                                escaped_url,
                                f'<a href="{escaped_url}" style="color:#58a6ff">{escaped_url}</a>'
                            )
                    details = f'<span style="color:#ffa657">submit</span> {formatted}'
                else:
                    details = _escape(sub_op) + " " + _escape(detail_text)
            elif op == "write_file":
                details = f'{_escape(e.get("file_name",""))} → {_escape(e.get("dest_dir",""))} ({e.get("byte_count",0)} bytes)'
            elif op in ("copy_project", "copy_file"):
                details = f'{_escape(e.get("local_src",""))} → {_escape(e.get("dest_dir",""))}'
            elif op == "test_environment":
                svcs = e.get("services", [])
                svc_names = [s.get("name", "?") for s in svcs]
                env_status = e.get("environment_status", "")
                details = f'services=[{", ".join(_escape(n) for n in svc_names)}] status={_escape(env_status)}'

            crossing = "crossing" if e.get("boundary_crossing") else ""

            rows_parts.append(
                f'<tr>'
                f'<td>{_escape(e.get("ts", ""))}</td>'
                f'<td class="op {cls} {crossing}">{_escape(op)}</td>'
                f'<td>{details}</td>'
                f'</tr>'
            )

        started = entries[0].get("ts", "") if entries else ""
        ended = entries[-1].get("ts", "") if entries else ""
        boundary_count = sum(
            1 for e in entries
            if e.get("boundary_crossing") or e.get("operation") == "boundary_crossing"
        )

        # Phase aggregation (per-run) and health classification (full-state:
        # the regression rule compares against other runs on the same repo).
        agg_state = _cached_agg_state()
        run_state = phase_state_for_run(agg_state, run_id)
        phase_view_html = _build_phase_view(run_state)
        health = classify_run_health(agg_state, run_id, _now_iso())
        health_cls = f"health-{health}" if health in HEALTH_ORDER else "health-progressing"

        html_content = _TRACE_HTML.format(
            run_id=run_id,
            started=started,
            ended=ended,
            op_count=len(entries),
            boundary_count=boundary_count,
            phase_view=phase_view_html,
            health=_escape(health),
            health_cls=health_cls,
            rows="\n".join(rows_parts),
            live_script=_live_script(
                view="trace",
                journal_offset=journal_offset,
                run_id=run_id,
            ),
        )
        self._send_html(html_content)


# ---------------------------------------------------------------------------
# Server manager
# ---------------------------------------------------------------------------


_dashboard_server: ThreadingHTTPServer | None = None
_dashboard_thread: threading.Thread | None = None
_dashboard_host: str = "127.0.0.1"
_dashboard_port: int = 8751


def start_dashboard(host: str = "127.0.0.1", port: int = 8751) -> str:
    """Start the web dashboard on *host*:*port* in a background thread.

    When *port* is 0, the OS assigns a free ephemeral port.
    Use :func:`get_dashboard_url` to retrieve the actual bound address.

    Threading (Issue #528): stopping a container takes a Docker round trip, and
    on a single-threaded server that request would stall every page load behind
    it -- including the ``/containers`` page the user is watching for the
    result.  ``ThreadingHTTPServer`` uses daemon threads, so shutdown behaviour
    is unchanged.

    Returns a status message.
    """
    global _dashboard_server, _dashboard_thread, _dashboard_host, _dashboard_port

    if _dashboard_server is not None:
        return f"Dashboard already running on http://{_dashboard_host}:{_dashboard_port}"

    _dashboard_host = host
    _dashboard_server = ThreadingHTTPServer((host, port), _DashboardHandler)
    _dashboard_port = _dashboard_server.server_address[1]
    _dashboard_thread = threading.Thread(
        target=_dashboard_server.serve_forever,
        daemon=True,
    )
    _dashboard_thread.start()
    return f"Dashboard started on http://{_dashboard_host}:{_dashboard_port}"


def get_dashboard_url() -> str | None:
    """Return the URL of the running dashboard, or None if not started."""
    if _dashboard_server is None:
        return None
    return f"http://{_dashboard_host}:{_dashboard_port}"


def stop_dashboard() -> str:
    """Stop the web dashboard if running."""
    global _dashboard_server, _dashboard_thread
    if _dashboard_server is None:
        return "Dashboard not running"
    _dashboard_server.shutdown()
    _dashboard_server = None
    _dashboard_thread = None
    return "Dashboard stopped"
