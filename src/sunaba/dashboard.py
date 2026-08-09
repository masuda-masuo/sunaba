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
import html as _html
import json
import secrets
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

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


# ---------------------------------------------------------------------------
# HTML template pages
# ---------------------------------------------------------------------------

#: Shared stylesheet.  Injected into the page templates as ``{style}`` rather
#: than inlined in each one, so ``/`` and ``/containers`` cannot drift apart
#: visually.  Because it is a *value* passed to ``str.format``, its braces are
#: not format placeholders and need no doubling.
_STYLE: str = """<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: system-ui, sans-serif; background: #0d1117; color: #c9d1d9; padding: 24px; }
h1 { font-size: 20px; color: #58a6ff; margin-bottom: 8px; }
.subtitle { color: #8b949e; font-size: 13px; margin-bottom: 16px; }
nav { display: flex; gap: 16px; margin-bottom: 24px; }
nav a { color: #58a6ff; font-size: 13px; text-decoration: none; }
nav a:hover { text-decoration: underline; }
nav a.active { color: #f0f6fc; font-weight: 600; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 16px; margin-bottom: 24px; }
.card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; }
.card h2 { font-size: 14px; color: #58a6ff; margin-bottom: 12px; border-bottom: 1px solid #21262d; padding-bottom: 8px; }
.card .meta { font-size: 12px; color: #8b949e; margin-bottom: 4px; }
.card .val { font-size: 24px; font-weight: 600; color: #f0f6fc; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
.badge.ok { background: #1b3820; color: #7ee787; }
.badge.err { background: #381620; color: #f97583; }
.badge.boundary { background: #382a10; color: #ffa657; }
.badge.svc-starting { background: #382a10; color: #ffa657; }
.badge.svc-ready { background: #1b3820; color: #7ee787; }
.badge.net-on { background: #1b3820; color: #7ee787; }
.badge.net-off { background: #21262d; color: #8b949e; }
.badge.net-unknown { background: #21262d; color: #484f58; }
.badge.kind-proxy { background: #382a10; color: #ffa657; }
.badge.health-done { background: #1b3820; color: #7ee787; }
.badge.health-looping { background: #381620; color: #f97583; }
.badge.health-stalled { background: #382a10; color: #ffa657; }
.badge.health-regression { background: #2d1b38; color: #d2a8ff; }
.badge.health-progressing { background: #21262d; color: #8b949e; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th, td { padding: 6px 10px; text-align: left; border-bottom: 1px solid #21262d; }
th { color: #8b949e; font-weight: 600; }
th.sortable { cursor: pointer; user-select: none; }
th.sortable:hover { color: #58a6ff; }
.pass { color: #7ee787; }
.fail { color: #f97583; }
.mono { font-family: monospace; font-size: 11px; }
.dim { color: #484f58; }
.stale { color: #ffa657; font-weight: 600; }
button { background: #21262d; border: 1px solid #30363d; color: #c9d1d9; padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 12px; }
button:hover { opacity: 0.8; }
button.danger { background: #21262d; border-color: #6e2c35; color: #f97583; padding: 3px 10px; font-size: 11px; }
button.danger:hover { background: #381620; opacity: 1; }
.empty { color: #484f58; font-style: italic; padding: 12px 0; }
.bar-wrap { display: flex; align-items: center; gap: 6px; margin: 1px 0; font-size: 11px; }
.bar-label { width: 90px; text-align: right; color: #8b949e; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bar-track { flex: 1; background: #21262d; border-radius: 3px; height: 14px; }
.bar-fill { display: block; height: 100%; border-radius: 3px; background: #58a6ff; }
.bar-num { width: 50px; text-align: right; color: #f0f6fc; font-family: monospace; }
.filter-form { display: flex; gap: 8px; align-items: center; margin-bottom: 10px; }
.filter-form input { background: #0d1117; border: 1px solid #30363d; color: #c9d1d9; padding: 3px 6px; font-size: 11px; border-radius: 4px; }
.filter-form button { background: #21262d; border: 1px solid #30363d; color: #c9d1d9; padding: 3px 10px; font-size: 11px; border-radius: 4px; cursor: pointer; }
.metric-row { margin-bottom: 6px; }
.metric-label { color: #f0f6fc; }
.metric-val { font-size: 18px; font-weight: 600; }
.metric-note { font-size: 11px; color: #484f58; }
details { font-size: 10px; color: #484f58; margin-top: 8px; }
.section-header { font-size: 11px; color: #8b949e; margin-bottom: 2px; margin-top: 8px; }
</style>"""

_NAV: str = """<nav>
  <a href="/" class="{home_cls}">Dashboard</a>
  <a href="/containers" class="{containers_cls}">Containers</a>
  <a href="/insights" class="{insights_cls}">Insights</a>
</nav>"""

_DASHBOARD_HTML: str = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Code Sandbox MCP — Dashboard</title>
{style}
</head>
<body>
<h1>Code Sandbox MCP</h1>
<div class="subtitle">Observability Dashboard — localhost only — live update</div>
{nav}

<div class="grid">
  <div class="card" id="stats-card">
    <h2>Stats</h2>
    {stats_card}
  </div>

  <div class="card" id="concurrency-card">
    <h2>Concurrent Containers</h2>
    {concurrency_card}
  </div>

  <div class="card" id="disk-card">
    <h2>Disk Usage</h2>
    {disk_card}
  </div>

  {tool_usage_panel}

  <div class="card" id="journal-card">
    <h2>Journal</h2>
    {journal_card}
  </div>

</div>

<h2 style="font-size: 16px; color: #8b949e; margin-bottom: 12px;">Recent Runs</h2>
<table>
<thead>
<tr>
  <th>Health</th>
  <th>Run ID</th>
  <th>Started</th>
  <th>Image</th>
  <th>Ops</th>
  <th>Crossings</th>
  <th>Status</th>
  <th>Trace</th>
</tr>
</thead>
<tbody id="run-rows">
{run_rows}
</tbody>
</table>
{live_script}
</body>
</html>"""
_RUN_ROW: str = """<tr>
  <td>{health}</td>
  <td class="mono">{run_id}</td>
  <td>{started}</td>
  <td class="mono">{image}</td>
  <td>{ops}</td>
  <td>{crossings}</td>
  <td><span class="badge {status_cls}">{status}</span></td>
  <td>
    <a href="/trace/{run_id}" style="color: #58a6ff; font-size: 11px;">HTML</a>
  </td>
</tr>"""

_CONTAINERS_HTML: str = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Code Sandbox MCP — Containers</title>
{style}
</head>
<body>
<h1>Containers</h1>
<div class="subtitle">Live from Docker (managed containers) — live update</div>
{nav}

<div id="stop-error">{stop_error}</div>
<div id="containers-error">{error}</div>

<table>
<thead>
<tr>
  <th>Name / ID</th>
  <th>Image</th>
  <th>Status</th>
  <th>Net</th>
  <th>Started (UTC)</th>
  <th>Idle</th>
  <th>Run</th>
  <th></th>
</tr>
</thead>
<tbody id="container-rows">
{rows}
</tbody>
</table>

<div id="sidecars">{sidecars}</div>
{live_script}
</body>
</html>"""

_CONTAINER_ROW: str = """<tr>
  <td>{name}<div class="mono dim">{cid}</div></td>
  <td class="mono">{image}</td>
  <td>{status_cell}</td>
  <td><span class="badge {net_cls}">{net}</span></td>
  <td>{created}</td>
  <td class="{idle_cls}">{idle}</td>
  <td>{run}</td>
  <td>
    <form method="post" action="/containers/stop">
      <input type="hidden" name="csrf" value="{csrf}">
      <input type="hidden" name="container_id" value="{cid}">
      <button type="submit" class="danger">Stop</button>
    </form>
  </td>
</tr>"""

#: Shown only when there is something to say.  A container with nothing
#: unpushed is stopped on the first click: an "are you sure?" with no content
#: is ceremony, and ceremony is what teaches people to click through warnings
#: (Issue #528).
_STOP_CONFIRM_HTML: str = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Code Sandbox MCP — Stop {cid}?</title>
{style}
</head>
<body>
<h1>Stop container?</h1>
<div class="subtitle">{cid}</div>
{nav}

<div class="card" style="max-width:640px">
  <h2><span class="badge boundary">warning</span> Unpushed work</h2>
  <div class="meta" style="margin-bottom:16px">{warning}</div>
  <div style="display:flex; gap:12px; align-items:center">
    <form method="post" action="/containers/stop">
      <input type="hidden" name="csrf" value="{csrf}">
      <input type="hidden" name="container_id" value="{cid}">
      <input type="hidden" name="force" value="true">
      <button type="submit" class="danger">Stop anyway</button>
    </form>
    <a href="/containers" style="color:#58a6ff; font-size:12px">Cancel</a>
  </div>
</div>
</body>
</html>"""

_TRACE_HTML: str = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Run Trace — {run_id}</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: system-ui, sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; }}
a {{ color: #58a6ff; }}
h1 {{ font-size: 18px; color: #58a6ff; margin-bottom: 16px; }}
.summary {{ display: flex; gap: 16px; margin-bottom: 20px; flex-wrap: wrap; }}
.badge {{ background: #21262d; border: 1px solid #30363d; border-radius: 6px; padding: 8px 14px; font-size: 13px; }}
.badge strong {{ color: #f0f6fc; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
th, td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid #21262d; }}
th {{ background: #161b22; color: #8b949e; }}
tr:hover {{ background: #161b22; }}
.op {{ font-weight: 600; }}
.op.initialize {{ color: #7ee787; }}
.op.exec {{ color: #a5d6ff; }}
.op.stop {{ color: #f97583; }}
.op.boundary_crossing {{ color: #ffa657; }}
.op.write_file {{ color: #d2a8ff; }}
.op.copy_project, .op.copy_file {{ color: #a5d6ff; }}
.op.test_environment {{ color: #7ee787; }}
.crossing {{ color: #ffa657; font-weight: 600; }}
.exit-ok {{ color: #7ee787; }}
.exit-err {{ color: #f97583; }}
.cmds {{ font-family: monospace; font-size: 12px; max-width: 500px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; display: inline-block; }}
.json-link {{ float: right; font-size: 12px; }}
.phase-view {{ margin-bottom: 20px; }}
.phase-line {{ display: flex; align-items: baseline; gap: 8px; padding: 5px 0; font-size: 13px; border-bottom: 1px solid #21262d; }}
.phase-name {{ font-weight: 600; min-width: 70px; }}
.phase-name.init {{ color: #7ee787; }}
.phase-name.explore {{ color: #a5d6ff; }}
.phase-name.edit {{ color: #d2a8ff; }}
.phase-name.verify {{ color: #ffa657; }}
.phase-name.publish {{ color: #f0883e; }}
.phase-name.other {{ color: #8b949e; }}
.phase-detail {{ color: #c9d1d9; }}
.phase-detail .dim {{ color: #484f58; }}
.phase-files {{ color: #8b949e; font-size: 12px; }}
.phase-check {{ font-size: 11px; }}
.phase-check.pass {{ color: #7ee787; }}
.phase-check.fail {{ color: #f97583; }}
.phase-roundtrip {{ color: #ffa657; font-size: 11px; margin-left: 8px; }}
.health-done {{ color: #7ee787; font-weight: 600; }}
.health-looping {{ color: #f97583; font-weight: 600; }}
.health-stalled {{ color: #ffa657; font-weight: 600; }}
.health-regression {{ color: #d2a8ff; font-weight: 600; }}
.health-progressing {{ color: #8b949e; font-weight: 600; }}
</style>
</head>
<body>
<a href="/">← Dashboard</a>
<h1>Run Trace — {run_id} <a class="json-link" href="/trace/{run_id}?fmt=json">JSON</a></h1>
<div class="summary">
  <div class="badge"><strong>Started:</strong> <span id="badge-started">{started}</span></div>
  <div class="badge"><strong>Ended:</strong> <span id="badge-ended">{ended}</span></div>
  <div class="badge"><strong>Operations:</strong> <span id="badge-ops">{op_count}</span></div>
  <div class="badge"><strong>Boundary crossings:</strong> <span id="badge-boundary">{boundary_count}</span></div>
  <div class="badge"><strong>Health:</strong> <span id="health-badge" class="{health_cls}">{health}</span></div>
</div>
<div id="phase-view">{phase_view}</div>
<table>
<thead>
<tr><th>Time</th><th>Operation</th><th>Details</th></tr>
</thead>
<tbody id="trace-rows">
{rows}
</tbody>
</table>
{live_script}
</body>
</html>"""



# ---------------------------------------------------------------------------
# Live update script (Issue #776)
# ---------------------------------------------------------------------------

#: Inline polling script injected into the live-updated pages.  It is a
#: *value* passed to ``str.format`` (as ``{live_script}``), never formatted
#: itself, so its braces are plain JS syntax; ``__VIEW__`` / ``__OFFSET__`` /
#: ``__RUN_ID__`` tokens are substituted by :func:`_live_script`.
#
# Security contract (mirrors the server's): journal-derived strings are
# inserted into the DOM with ``textContent`` only.  The single ``innerHTML``
# uses are for fragments rendered by the server's own view functions, which
# HTML-escape everything they interpolate -- the same trust level as the
# initial page load.
_LIVE_SCRIPT: str = """<script>
(function () {
  "use strict";
  var POLL_MS = 1500;
  var view = __VIEW__;
  var offset = __OFFSET__;
  var runId = __RUN_ID__;

  // Trace-page counters, seeded from the server-rendered summary badges.
  var ops = 0;
  var crossings = 0;

  function setText(id, text) {
    var el = document.getElementById(id);
    if (el) { el.textContent = text; }
  }

  function entryDetails(e) {
    var op = e.operation || "";
    if (op === "initialize") {
      return "image=" + (e.image || "") + " net=" + (e.allow_network === undefined ? "" : e.allow_network);
    }
    if (op === "exec") {
      return (e.commands || []).join(" && ") + " exit=" + (e.exit_code === undefined ? "" : e.exit_code);
    }
    if (op === "boundary_crossing") {
      return (e.sub_operation || "") + " " + (e.details || "");
    }
    if (op === "write_file") {
      return (e.file_name || "") + " \\u2192 " + (e.dest_dir || "") + " (" + (e.byte_count || 0) + " bytes)";
    }
    if (op === "copy_project" || op === "copy_file") {
      return (e.local_src || "") + " \\u2192 " + (e.dest_dir || "");
    }
    if (op === "test_environment") {
      var svcs = (e.services || []).map(function (s) { return s.name || "?"; }).join(", ");
      return "services=[" + svcs + "] status=" + (e.environment_status || "");
    }
    if (op === "tool_use") {
      return e.tool_name || "";
    }
    return "";
  }

  // Every value is a journal string, so it goes in via textContent.
  function rowForEntry(e) {
    var tr = document.createElement("tr");
    var tdTs = document.createElement("td");
    tdTs.textContent = e.ts || "";
    var tdOp = document.createElement("td");
    tdOp.textContent = e.operation || "unknown";
    tdOp.className = "op " + (e.operation || "unknown");
    if (e.boundary_crossing) { tdOp.className += " crossing"; }
    var tdDet = document.createElement("td");
    tdDet.textContent = entryDetails(e);
    tr.appendChild(tdTs);
    tr.appendChild(tdOp);
    tr.appendChild(tdDet);
    return tr;
  }

  function seedCounters() {
    var o = document.getElementById("badge-ops");
    var b = document.getElementById("badge-boundary");
    ops = o ? (parseInt(o.textContent, 10) || 0) : 0;
    crossings = b ? (parseInt(b.textContent, 10) || 0) : 0;
  }

  function applyTrace(data) {
    var tbody = document.getElementById("trace-rows");
    (data.entries || []).forEach(function (e) {
      if (e.run_id !== runId) { return; }
      ops += 1;
      if (e.boundary_crossing || e.operation === "boundary_crossing") { crossings += 1; }
      if (tbody) { tbody.appendChild(rowForEntry(e)); }
      if (e.ts) { setText("badge-ended", e.ts); }
    });
    setText("badge-ops", String(ops));
    setText("badge-boundary", String(crossings));
    // Server-rendered fragments: phase view (escaped HTML) and the health
    // badge (plain label + class), recomputed over the full journal.
    if (data.phase_view !== undefined) {
      var pv = document.getElementById("phase-view");
      if (pv) { pv.innerHTML = data.phase_view; }
    }
    if (data.health !== undefined) {
      var hb = document.getElementById("health-badge");
      if (hb) {
        hb.textContent = data.health;
        hb.className = data.health_cls || "";
      }
    }
  }

  function applyDashboard(data) {
    if (data.stats_card !== undefined) {
      var sc = document.getElementById("stats-card");
      if (sc) { sc.innerHTML = data.stats_card; }
    }
    if (data.concurrency_card !== undefined) {
      var cc = document.getElementById("concurrency-card");
      if (cc) { cc.innerHTML = data.concurrency_card; }
    }
    if (data.disk_card !== undefined) {
      var dc = document.getElementById("disk-card");
      if (dc) { dc.innerHTML = data.disk_card; }
    }
    if (data.journal_card !== undefined) {
      var jc = document.getElementById("journal-card");
      if (jc) { jc.innerHTML = data.journal_card; }
    }
    if (data.run_rows !== undefined) {
      var rr = document.getElementById("run-rows");
      if (rr) { rr.innerHTML = data.run_rows; }
    }
  }

  function applyContainers(data) {
    var rows = document.getElementById("container-rows");
    if (rows && data.rows_html !== undefined) { rows.innerHTML = data.rows_html; }
    var sc = document.getElementById("sidecars");
    if (sc && data.sidecars_html !== undefined) { sc.innerHTML = data.sidecars_html; }
    var err = document.getElementById("containers-error");
    if (err && data.error_html !== undefined) { err.innerHTML = data.error_html; }
  }

  var gen = null;
  var inFlight = false;

  function poll() {
    // In-flight guard: a poll slower than the interval must not overlap the
    // next one -- two concurrent polls would read from the same offset and
    // append the same trace rows twice.
    if (inFlight) { return; }
    inFlight = true;
    var url = "/api/journal?offset=" + offset + "&view=" + view;
    if (gen !== null) { url += "&gen=" + gen; }
    if (runId !== null) { url += "&run_id=" + encodeURIComponent(runId); }
    fetch(url)
      .then(function (r) {
        if (!r.ok) { throw new Error("journal poll failed: " + r.status); }
        return r.json();
      })
      .then(function (data) {
        inFlight = false;
        if (data.generation !== undefined) { gen = data.generation; }
        if (data.rotated) {
          // Rotation: the live file was replaced by journal.log.1 and a new
          // journal.log started, so our byte offset no longer exists.  Reset
          // to 0; the next poll re-reads the new file from the top and the
          // server re-renders every fragment from the full history (backup
          // included), so the display re-syncs without a page reload.
          offset = 0;
          return;
        }
        offset = data.next_offset;
        if (view === "trace") { applyTrace(data); }
        else if (view === "containers") { applyContainers(data); }
        else if (view === "dashboard") { applyDashboard(data); }
      })
      .catch(function () { inFlight = false; /* transient error: keep polling */ });
  }

  seedCounters();
  setInterval(poll, POLL_MS);
})();
</script>"""


def _js_string(value: str) -> str:
    """Encode *value* as a JS string literal safe to embed in ``<script>``.

    ``json.dumps`` quotes and escapes correctly for JS but leaves ``<`` / ``>``
    literal, and a literal ``</script>`` inside the string would terminate the
    script element early (the HTML parser does not understand JS strings).
    ``<``, ``>`` and ``&`` are therefore re-escaped as ``\\uXXXX``, which JS
    decodes to the identical characters.
    """
    return (
        json.dumps(value)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _live_script(
    *,
    view: str,
    journal_offset: int,
    run_id: str | None = None,
) -> str:
    """Return the live-update script for a page (Issue #776).

    *view* is one of ``"trace"`` / ``"containers"`` / ``"dashboard"``.
    *journal_offset* is the byte size of the live journal at render time, so
    the first poll only returns entries written after this page was served.
    *run_id* is set on the trace page so the server can include the phase
    view and health badge fragments.

    Token substitution (``__VIEW__`` etc.) keeps the script body free of
    ``str.format`` placeholders -- its braces are JS syntax, not template
    slots.
    """
    script = _LIVE_SCRIPT
    script = script.replace("__VIEW__", _js_string(view))
    script = script.replace("__OFFSET__", str(int(journal_offset)))
    script = script.replace("__RUN_ID__", _js_string(run_id) if run_id else "null")
    return script


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Current UTC time in the journal's ISO-8601 format (``Z`` suffix)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _render_health_badge(health: str) -> str:
    """Render a run-health badge (Issue #775).

    *health* is one of :data:`sunaba.health.HEALTH_ORDER`; unknown values
    fall back to the neutral ``progressing`` style.
    """
    cls = f"health-{health}" if health in HEALTH_ORDER else "health-progressing"
    return f'<span class="badge {cls}">{_escape(health)}</span>'


def _escape(text: str) -> str:
    """HTML-escape *text* for safe embedding in attribute values and text content.

    Uses ``html.escape(quote=True)`` which handles ``&``, ``<``, ``>``,
    ``"``, and ``'`` — sufficient for ``title`` and ``value`` attributes.
    Not suitable for ``href``, ``style``, ``on*``, or raw URL contexts.
    """
    return _html.escape(text, quote=True)


def _build_phase_view(run_state: dict[str, Any] | None) -> str:
    """Render the aggregated phase view for a run as HTML.

    Returns an empty string when *run_state* is ``None`` or has no phases.
    """
    if not run_state or not run_state.get("phases"):
        return ""

    phases = run_state["phases"]
    lines: list[str] = []

    # ── header ──
    img = _escape(_short_image(run_state.get("image", "unknown")))
    sl = run_state.get("session_label")
    label_str = f" · {_escape(sl)}" if sl else ""
    repo = run_state.get("repo")
    repo_str = f" · {_escape(repo)}" if repo else ""
    lines.append(
        f'<div style="font-size:12px;color:#8b949e;margin-bottom:6px">'
        f'{img}{label_str}{repo_str}</div>'
    )

    # ── phase lines ──
    for seg in phases:
        p = seg["phase"]
        count = seg["op_count"]
        dur = seg.get("duration_s")
        dur_str = _fmt_duration(dur) if dur else ""
        bd = seg.get("breakdown", {})

        # Per-operation breakdown (compact)
        bd_parts: list[str] = []
        for op_key, n in sorted(bd.items(), key=lambda x: -x[1]):
            # Shorten tool: and boundary: prefixes
            short = op_key
            if op_key.startswith("tool:"):
                short = op_key[5:]
            elif op_key.startswith("boundary:"):
                short = op_key[9:]
            bd_parts.append(f"{_escape(short)} {n}")
        bd_str = " / ".join(bd_parts) if bd_parts else ""

        dur_html = f' <span class="dim">{dur_str}</span>' if dur_str else ""

        line = (
            f'<div class="phase-line">'
            f'<span class="phase-name {p}">{p}</span>'
            f'<span class="phase-detail">{count} ops'
        )
        if bd_str:
            line += f' <span class="dim">({bd_str})</span>'
        line += f'{dur_html}</span>'

        # Verify check marks
        if p == "verify":
            vt = run_state.get("verify_timeline", [])
            vs = seg.get("start_ts", "")
            ve = seg.get("end_ts", "")

            # Collect outcomes from pipeline runs and verify_outcome entries.
            passes = 0
            fails = 0
            check_marks: list[str] = []
            for vt_entry in vt:
                vts = vt_entry.get("ts", "")
                if not (vs <= vts <= ve):
                    continue
                vtype = vt_entry.get("type", "")
                if vtype in ("pytest_run", "verify_outcome"):
                    passed = vt_entry.get("passed")
                    if passed is True:
                        passes += 1
                        check_marks.append(
                            ' <span class="phase-check pass">\u2713</span>'
                        )
                    elif passed is False:
                        fails += 1
                        check_marks.append(
                            ' <span class="phase-check fail">\u2717</span>'
                        )
            line += "".join(check_marks)

            # Show pass/fail counts
            if passes or fails:
                parts = []
                if passes:
                    parts.append(f'<span class="pass">{passes} pass</span>')
                if fails:
                    parts.append(f'<span class="fail">{fails} fail</span>')
                line += " " + " ".join(parts)

        line += "</div>"
        lines.append(line)

    # ── touched files ──
    touched = run_state.get("touched_files", [])
    if touched:
        files_str = ", ".join(_escape(f) for f in touched[:20])
        if len(touched) > 20:
            files_str += f" ... ({len(touched)} total)"
        lines.append(
            f'<div class="phase-files" style="margin-top:4px">'
            f'\u2192 touched: {files_str}'
            f'</div>'
        )

    # ── edit-verify roundtrips ──
    rtrips = run_state.get("edit_verify_roundtrips", 0)
    if rtrips:
        lines.append(
            f'<div class="phase-roundtrip">'
            f'{rtrips} edit \u2192 verify-fail \u2192 edit roundtrip(s)'
            f'</div>'
        )

    if not lines:
        return ""
    return '<div class="phase-view">' + "\n".join(lines) + "</div>"


def _render_bar(n: int, max_val: int, label: str, color: str = "#58a6ff") -> str:
    pct = round(n / max_val * 100) if max_val > 0 else 0
    return (
        f'<div class="bar-wrap">'
        f'<span class="bar-label" title="{_escape(label)}">{_escape(label)}</span>'
        f'<span class="bar-track">'
        f'<span class="bar-fill" style="background:{color};width:{pct}%"></span>'
        f'</span>'
        f'<span class="bar-num">{n} ({pct}%)</span>'
        f'</div>'
    )


def _render_cd_rate_row(usage: dict[str, Any]) -> str:
    """Render the cd-rate metric row, split by redundant cds (Issue #845).

    ``cd_rate_pct`` still counts every leading cd, so the number stays
    comparable with history; the redundant share -- cds to the default
    working directory, which are no-ops -- is the actionable half.  A
    usage dict predating #845 carries neither redundant key, so both
    default to 0 rather than raising.
    """
    redundant_count = usage.get("cd_redundant_count", 0)
    redundant_pct = usage.get("cd_redundant_rate_pct", 0)
    return (
        f'<div class="metric-row">'
        f'<span class="metric-label">cd rate:</span> '
        f'<span class="metric-val" style="color:#ffa657">{usage["cd_rate_pct"]}%</span> '
        f'<span class="metric-note">({usage["cd_count"]} / {usage["exec_entry_count"]} exec entries'
        f'; of which redundant &rarr;/workspace: {redundant_pct}%, {redundant_count})</span>'
        f'</div>'
    )


def _render_tool_usage_panel(
    from_date: str | None,
    to_date: str | None,
) -> str:
    """Render the tool usage panel HTML (Issue #229)."""
    usage = get_tool_usage(from_date=from_date, to_date=to_date)

    time_from = _escape(usage["time_range"]["from"][:10])
    time_to = _escape(usage["time_range"]["to"][:10])
    if time_to.endswith("T00:00:00"):
        time_to = time_to.split("T")[0]

    # Date filter form
    filter_html = (
        f'<div class="filter-form">'
        f'<form method="get" action="/" style="display:flex;gap:8px;align-items:center">'
        f'<input type="date" name="tool_from" value="{time_from}">'
        f'<span style="color:#484f58;font-size:11px">to</span>'
        f'<input type="date" name="tool_to" value="{time_to}">'
        f'<button type="submit">Apply</button>'
        f'</form>'
        f'</div>'
    )

    # Exec share
    exec_share_color = "#ffa657" if usage["exec_share_pct"] > 50 else "#7ee787"
    exec_html = (
        f'<div class="metric-row">'
        f'<span class="metric-label">exec share:</span> '
        f'<span class="metric-val" style="color:{exec_share_color}">{usage["exec_share_pct"]}%</span> '
        f'<span class="metric-note">({usage["exec_ops"]} / {usage["total_ops"]} ops)</span>'
        f'</div>'
    )

    # CD rate
    cd_html = _render_cd_rate_row(usage)

    # Bypass rate
    bypass_total = usage["bypass_count"]
    struct_total = sum(
        n for k, n in usage["structured_ops"].items()
        if not k.startswith("boundary:")
    )
    bypass_denom = bypass_total + struct_total
    bypass_color = "#f97583" if usage["bypass_rate_pct"] > 20 else "#7ee787"
    bypass_html = (
        f'<div class="metric-row">'
        f'<span class="metric-label">bypass rate:</span> '
        f'<span class="metric-val" style="color:{bypass_color}">{usage["bypass_rate_pct"]}%</span> '
        f'<span class="metric-note">({bypass_total} bypass / {bypass_denom} total)</span>'
        f'</div>'
    )

    # Command buckets
    buckets = usage["command_buckets"]
    total_exec = usage["exec_entry_count"]
    bucket_bars = ""
    if buckets and total_exec:
        for label, count in sorted(buckets.items(), key=lambda x: -x[1]):
            bucket_bars += _render_bar(count, total_exec, label)
    else:
        bucket_bars = '<div class="empty">No exec entries</div>'
    buckets_html = (
        f'<div class="section-header">Exec command buckets ({total_exec} entries):</div>'
        f'{bucket_bars}'
    )

    # Structured tool ops
    structured = usage["structured_ops"]
    struct_bars = ""
    struct_counts = [
        (k, n) for k, n in structured.items() if not k.startswith("boundary:")
    ]
    if struct_counts:
        struct_max = max(n for _, n in struct_counts)
        for label, count in sorted(struct_counts, key=lambda x: -x[1]):
            struct_bars += _render_bar(count, struct_max, label, color="#7ee787")
        struct_bars_display = struct_bars
    else:
        struct_bars_display = '<div class="empty">No structured ops</div>'
    struct_html = (
        f'<div class="section-header">Structured tool ops ({struct_total} total):</div>'
        f'{struct_bars_display}'
    )

    # Bypass detail
    bypass_detail = usage["bypass_detail"]
    bypass_detail_bars = ""
    if bypass_detail:
        bypass_max = max(bypass_detail.values())
        for cmd, count in sorted(bypass_detail.items(), key=lambda x: -x[1]):
            bypass_detail_bars += _render_bar(count, bypass_max, f"shell:{cmd}", color="#f97583")

    bypass_detail_html = ""
    if bypass_detail_bars:
        bypass_detail_html = (
            f'<div class="section-header">Bypass by shell command:</div>'
            f'{bypass_detail_bars}'
        )

    # Tool intro dates
    intro_dates = usage.get("_tool_intro_dates", {})
    intro_lines = ""
    for tool_name, intro_date in sorted(intro_dates.items(), key=lambda x: x[1]):
        intro_lines += (
            f'<div style="font-size:10px;color:#484f58">'
            f'{_escape(tool_name)}: {_escape(intro_date)}'
            f'</div>'
        )

    return (
        f'<div class="card" id="tool-usage-card">'
        f'<h2>Tool Usage '
        f'<span style="font-size:11px;color:#8b949e;font-weight:400;">'
        f'(sandbox_exec dependency | #229)'
        f'</span></h2>'
        f'{filter_html}'
        f'{exec_html}'
        f'{cd_html}'
        f'{bypass_html}'
        f'{buckets_html}'
        f'{bypass_detail_html}'
        f'{struct_html}'
        f'<details><summary>Tool intro dates (bias control)</summary>'
        f'{intro_lines}'
        f'</details>'
        f'</div>'
    )


#: Idle time past which a container is highlighted as probably forgotten.
#: Three hours is well beyond a working session's natural pauses, and well
#: short of the 21-hour strays that motivated Issue #527.
_STALE_IDLE_SECONDS: float = 3 * 3600


def _render_nav(active: str) -> str:
    return _NAV.format(
        home_cls="active" if active == "home" else "",
        containers_cls="active" if active == "containers" else "",
        insights_cls="active" if active == "insights" else "",
    )


def _short_image(image: str) -> str:
    """Collapse a pinned digest so the image column stays readable."""
    if "@sha256:" in image:
        return image.split("@sha256:")[0] + "@sha256:..."
    return image


def _fmt_duration(seconds: float | None) -> str:
    """Render a duration compactly: ``45s`` / ``12m`` / ``21.3h``."""
    if seconds is None:
        return "\u2014"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    return f"{seconds / 3600:.1f}h"


def _fmt_bytes(n: int | None) -> str:
    """Render a byte count compactly: ``512 B`` / ``12.3 MB`` / ``4.1 GB``."""
    if n is None:
        return "\u2014"
    for unit, div in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{int(n)} B"


def _fmt_hhmm(ts: str) -> str:
    """Render an ISO timestamp as ``HH:MM:SS`` (UTC), or ``\u2014``."""
    if not ts:
        return "\u2014"
    return ts[11:19] if len(ts) >= 19 else ts


def _render_status_cell(status: str, env: dict[str, Any] | None) -> str:
    """Render the Docker status, plus the test-environment services if any.

    The services come from the journal's ``test_environment`` records -- the
    same source the old "Active Environments" panel used.  They stay on the
    page, but now hang off the container that actually exists rather than
    standing in for the container list (Issue #527).
    """
    status_cls = "ok" if status == "running" else "err"
    cell = f'<span class="badge {status_cls}">{_escape(status)}</span>'
    if not env:
        return cell
    env_status = env.get("environment_status", "unknown")
    env_cls = "svc-ready" if env_status == "ready" else "svc-starting"
    svc_names = ", ".join(s.get("name", "?") for s in env.get("services", []))
    return (
        f'{cell}'
        f'<div style="margin-top:4px">'
        f'<span class="badge {env_cls}">{_escape(env_status)}</span> '
        f'<span class="dim">{_escape(svc_names)}</span>'
        f'</div>'
    )


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


def _render_sidecars(sidecars: list[dict[str, Any]]) -> str:
    """Render the egress-proxy sidecar(s) below the table, out of the way.

    They are managed containers too, so they show up in the same Docker query,
    but they are infrastructure rather than workspaces -- listing them beside
    the sandboxes is what made the old view confusing.
    """
    if not sidecars:
        return ""
    items = "".join(
        f'<div class="meta">'
        f'<span class="badge kind-proxy">proxy</span> '
        f'<span class="mono">{_escape(s.get("container_id", ""))}</span> '
        f'<span class="dim">{_escape(_short_image(str(s.get("image", ""))))}</span> '
        f'({_escape(str(s.get("status", "")))})'
        f'</div>'
        for s in sidecars
    )
    return (
        f'<div class="card" style="margin-top:24px">'
        f'<h2>Sidecars</h2>{items}'
        f'</div>'
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


def _strip_error_prefix(message: str) -> str:
    """Drop the ``Error:`` prefix the tool layer adds for its LLM caller.

    The page says "error" in a badge next to it, so the word is redundant here.
    """
    return message.removeprefix("Error: ")


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



# ──────────────────────────────────────────────────────────────────
# Insights page (#777)
# ──────────────────────────────────────────────────────────────────

_INSIGHTS_HTML: str = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Code Sandbox MCP — Insights</title>
{style}
</head>
<body>
<h1>Insights</h1>
<div class="subtitle">Cross-run friction metrics — all numbers, no gut feel</div>
{nav}

<div class="filter-form" style="margin-bottom:16px">
  <form method="get" action="/insights" style="display:flex;gap:8px;align-items:center">
    <span style="color:#8b949e;font-size:11px">Period:</span>
    <a href="/insights" style="color:#58a6ff;font-size:11px;text-decoration:none;padding:2px 6px;border-radius:4px;{all_cls}">all time</a>
    <a href="/insights?period=7" style="color:#58a6ff;font-size:11px;text-decoration:none;padding:2px 6px;border-radius:4px;{d7_cls}">7 days</a>
    <a href="/insights?period=30" style="color:#58a6ff;font-size:11px;text-decoration:none;padding:2px 6px;border-radius:4px;{d30_cls}">30 days</a>
  </form>
</div>

<div class="grid">
  {error_rate_panel}
  {first_verify_panel}
</div>

<div class="grid">
  {roundtrip_panel}
  {unused_panel}
</div>

{run_dist_panel}

<div class="grid">
  {init_duration_panel}
  {busy_refusals_panel}
</div>

</body>
</html>"""


def _render_insights_page(
    insights: dict[str, Any],
    period_days: int | None,
) -> str:
    """Render the /insights page from computed metrics."""
    d = insights

    # Period filter styling
    all_cls = "border:1px solid #30363d;background:#21262d;"
    d7_cls = "border:1px solid #30363d;background:#21262d;"
    d30_cls = "border:1px solid #30363d;background:#21262d;"
    active_cls = "border:1px solid #58a6ff;background:#1f2b3f;"
    if period_days is None:
        all_cls = active_cls
    elif period_days == 7:
        d7_cls = active_cls
    elif period_days == 30:
        d30_cls = active_cls

    # ── Metric 1: Per-tool error rate ──
    err = d["per_tool_error_rate"]
    err_rows: list[str] = []
    for tool in err["by_tool"]:
        rate_color = "#7ee787" if tool["failure_rate"] == 0 else (
            "#ffa657" if tool["failure_rate"] < 0.2 else "#f97583"
        )
        err_rows.append(
            f'<tr>'
            f'<td class="mono">{_escape(tool["operation"])}</td>'
            f'<td>{tool["calls"]}</td>'
            f'<td>{tool["failures"]}</td>'
            f'<td style="color:{rate_color}">{tool["failure_rate"]:.1%}</td>'
            f'</tr>'
        )
    err_table = (
        f'<table><thead><tr>'
        f'<th>Operation</th><th>Calls</th><th>Failures</th><th>Rate</th>'
        f'</tr></thead><tbody>{"".join(err_rows)}</tbody></table>'
        if err_rows else '<div class="empty">No tool calls recorded</div>'
    )

    # Recovery distribution (sub-table)
    recovery_html = ""
    if err.get("recovery_distribution"):
        rec_parts: list[str] = []
        for failed_op in sorted(err["recovery_distribution"].keys()):
            next_ops = err["recovery_distribution"][failed_op]
            for next_op in sorted(next_ops.keys(), key=lambda k: -next_ops[k]):
                rec_parts.append(
                    f'<div style="font-size:10px;color:#8b949e">'
                    f'{_escape(failed_op)} → {_escape(next_op)}: {next_ops[next_op]}'
                    f'</div>'
                )
        if rec_parts:
            recovery_html = (
                f'<details><summary>Recovery actions (next op after failure)</summary>'
                f'{"".join(rec_parts)}'
                f'</details>'
            )

    error_rate_panel = (
        f'<div class="card">'
        f'<h2>1. Per-Tool Error Rate '
        f'<span style="font-size:11px;color:#8b949e;font-weight:400">'
        f'({err["total_calls"]} calls, {err["total_failures"]} failures)'
        f'</span></h2>'
        f'{err_table}'
        f'{recovery_html}'
        f'</div>'
    )

    # ── Metric 2: First-verify failure rate by image ──
    fv = d["first_verify_failure_by_image"]
    fv_rows: list[str] = []
    for img in fv["by_image"]:
        rate_color = "#7ee787" if img["failure_rate"] == 0 else (
            "#ffa657" if img["failure_rate"] < 0.3 else "#f97583"
        )
        fv_rows.append(
            f'<tr>'
            f'<td class="mono">{_escape(img["image"])}</td>'
            f'<td>{img["total_runs"]}</td>'
            f'<td>{img["first_verify_failed"]}</td>'
            f'<td style="color:{rate_color}">{img["failure_rate"]:.1%}</td>'
            f'</tr>'
        )
    overall = fv["overall"]
    fv_table = (
        f'<table><thead><tr>'
        f'<th>Image</th><th>Runs</th><th>Failed</th><th>Rate</th>'
        f'</tr></thead><tbody>{"".join(fv_rows)}</tbody></table>'
        if fv_rows else '<div class="empty">No runs with verify data</div>'
    )

    first_verify_panel = (
        f'<div class="card">'
        f'<h2>2. First-Verify Failure by Image '
        f'<span style="font-size:11px;color:#8b949e;font-weight:400">'
        f'(overall: {overall["total_first_failed"]}/{overall["total_runs_with_verify"]} = {overall["failure_rate"]:.1%})'
        f'</span></h2>'
        f'{fv_table}'
        f'</div>'
    )

    # ── Metric 3: Roundtrip distribution ──
    rd = d["roundtrip_distribution"]
    max_count = max(item["count"] for item in rd["histogram"]) if rd["histogram"] else 1
    rt_bars = ""
    for item in rd["histogram"]:
        rt_bars += _render_bar(item["count"], max_count, item["bucket"], color="#d2a8ff")
    rt_mean_html = (
        f'<div style="font-size:12px;color:#f0f6fc;margin-top:8px">'
        f'Mean: {rd["mean_roundtrips"]} roundtrips/run ({rd["total_runs"]} runs)'
        f'</div>'
    )
    roundtrip_panel = (
        f'<div class="card">'
        f'<h2>3. Edit→Verify Roundtrip Distribution</h2>'
        f'{rt_bars}'
        f'{rt_mean_html}'
        f'</div>'
    )

    # ── Metric 4: Unused tools ──
    unused = d["unused_tools"]
    unused_rows: list[str] = []
    for item in unused:
        unused_rows.append(
            f'<tr><td class="mono">{_escape(item["operation"])}</td>'
            f'<td class="dim">{_escape(item.get("reason", ""))}</td></tr>'
        )
    unused_table = (
        f'<table><thead><tr><th>Operation</th><th>Reason</th></tr></thead>'
        f'<tbody>{"".join(unused_rows)}</tbody></table>'
        if unused_rows else '<div class="empty">All known tools have been used</div>'
    )
    unused_panel = (
        f'<div class="card">'
        f'<h2>4. Unused Tools '
        f'<span style="font-size:11px;color:#8b949e;font-weight:400">'
        f'({len(unused)} candidate{"" if len(unused) == 1 else "s"})'
        f'</span></h2>'
        f'{unused_table}'
        f'</div>'
    )

    # ── Metric 5: Run duration & op-count distributions ──
    rdists = d["run_distributions"]

    # By repo
    repo_rows: list[str] = []
    for item in rdists.get("by_repo", []):
        ds = item["duration_stats"]
        os_ = item["op_count_stats"]
        repo_rows.append(
            f'<tr>'
            f'<td class="mono">{_escape(item["key"])}</td>'
            f'<td>{item["run_count"]}</td>'
            f'<td>{_fmt_duration(ds["min"])} – {_fmt_duration(ds["max"])} (mean {_fmt_duration(ds["mean"])})</td>'
            f'<td>{os_["min"]} – {os_["max"]} (mean {os_["mean"]:.1f})</td>'
            f'</tr>'
        )
    repo_table = (
        f'<table><thead><tr><th>Repo</th><th>Runs</th><th>Duration</th><th>Ops</th></tr></thead>'
        f'<tbody>{"".join(repo_rows)}</tbody></table>'
        if repo_rows else '<div class="empty">No run data</div>'
    )

    # By session_label
    label_rows: list[str] = []
    for item in rdists.get("by_session_label", []):
        ds = item["duration_stats"]
        os_ = item["op_count_stats"]
        label_rows.append(
            f'<tr>'
            f'<td class="mono">{_escape(item["key"])}</td>'
            f'<td>{item["run_count"]}</td>'
            f'<td>{_fmt_duration(ds["min"])} – {_fmt_duration(ds["max"])} (mean {_fmt_duration(ds["mean"])})</td>'
            f'<td>{os_["min"]} – {os_["max"]} (mean {os_["mean"]:.1f})</td>'
            f'</tr>'
        )
    label_table = (
        f'<table><thead><tr><th>Session</th><th>Runs</th><th>Duration</th><th>Ops</th></tr></thead>'
        f'<tbody>{"".join(label_rows)}</tbody></table>'
        if label_rows else '<div class="empty">No run data</div>'
    )

    run_dist_panel = (
        f'<div class="grid">'
        f'<div class="card">'
        f'<h2>5a. Run Distribution by Repo</h2>'
        f'{repo_table}'
        f'</div>'
        f'<div class="card">'
        f'<h2>5b. Run Distribution by Session Label</h2>'
        f'{label_table}'
        f'</div>'
        f'</div>'
    )

    # \u2500\u2500 Metric 6 (Issue #783): initialize duration distribution \u2500\u2500
    initd = d["initialize_duration_distribution"]
    init_stats = initd["stats"]
    max_init_count = max(
        (item["count"] for item in initd["histogram"]), default=1
    )
    init_bars = ""
    for item in initd["histogram"]:
        init_bars += _render_bar(item["count"], max_init_count, item["bucket"], color="#7ee787")
    init_summary = (
        f'<div style="font-size:12px;color:#f0f6fc;margin-top:8px">'
        f'{init_stats["count"]} completed inits \u00b7 mean {_fmt_duration(init_stats["mean"])} '
        f'\u00b7 median {_fmt_duration(init_stats["median"])} '
        f'\u00b7 max {_fmt_duration(init_stats["max"])}'
        f'</div>'
    )
    if initd["abandoned"]:
        init_summary += (
            f'<div style="font-size:11px;color:#ffa657;margin-top:4px">'
            f'{initd["abandoned"]} run(s) stopped without completing initialize '
            f'(abandoned init)'
            f'</div>'
        )
    if initd["in_flight"]:
        init_summary += (
            f'<div style="font-size:11px;color:#8b949e;margin-top:4px">'
            f'{initd["in_flight"]} run(s) with initialize not (yet) completed '
            f'&mdash; in flight or ended without a stop record'
            f'</div>'
        )
    init_duration_panel = (
        f'<div class="card">'
        f'<h2>6. Initialize Duration</h2>'
        f'{init_bars}'
        f'{init_summary}'
        f'</div>'
    )

    # \u2500\u2500 Metric 7 (Issue #783): per-pool busy refusals (initialize-wait proxy) \u2500\u2500
    busy = d["busy_refusal_counts"]
    busy_rows: list[str] = []
    for item in busy["by_pool"]:
        pool_color = "#f97583" if item["count"] > 0 else "#8b949e"
        busy_rows.append(
            f'<tr><td class="mono">{_escape(item["pool"])}</td>'
            f'<td style="color:{pool_color}">{item["count"]}</td></tr>'
        )
    busy_table = (
        f'<table><thead><tr><th>Pool</th><th>Refusals</th></tr></thead>'
        f'<tbody>{"".join(busy_rows)}</tbody></table>'
        if busy_rows else '<div class="empty">No busy refusals recorded</div>'
    )
    busy_refusals_panel = (
        f'<div class="card">'
        f'<h2>7. Busy Refusals by Pool '
        f'<span style="font-size:11px;color:#8b949e;font-weight:400">'
        f'(total {busy["total"]})'
        f'</span></h2>'
        f'{busy_table}'
        f'<div class="metric-note" style="margin-top:6px">'
        f'Concurrency-cap refusals (#784) \u2014 the observable form of '
        f'"initialize wait" (non-blocking acquire refuses instead of queueing)'
        f'</div>'
        f'</div>'
    )

    return _INSIGHTS_HTML.format(
        style=_STYLE,
        nav=_render_nav("insights"),
        all_cls=all_cls,
        d7_cls=d7_cls,
        d30_cls=d30_cls,
        error_rate_panel=error_rate_panel,
        first_verify_panel=first_verify_panel,
        roundtrip_panel=roundtrip_panel,
        unused_panel=unused_panel,
        run_dist_panel=run_dist_panel,
        init_duration_panel=init_duration_panel,
        busy_refusals_panel=busy_refusals_panel,
    )



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
