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

import html as _html
import json
import secrets
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from sunaba.health import HEALTH_ORDER, classify_all_runs, classify_run_health
from sunaba.insights import compute_all_insights
from sunaba.journal import (
    get_active_environments,
    get_journal_path,
    get_run_id_per_container,
    get_runs,
    get_tool_usage,
    read_journal,
)
from sunaba.phase import _PHASE_MAP, aggregate_run_phases, phase_state_for_run
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
<meta http-equiv="refresh" content="10">
<title>Code Sandbox MCP — Dashboard</title>
{style}
</head>
<body>
<h1>Code Sandbox MCP</h1>
<div class="subtitle">Observability Dashboard — localhost only — auto-refresh 10s</div>
{nav}

<div class="grid">
  <div class="card">
    <h2>Stats</h2>
    <div class="meta">Total Runs</div>
    <div class="val">{total_runs}</div>
    <div class="meta" style="margin-top:8px">Total Operations</div>
    <div class="val">{total_ops}</div>
    <div class="meta" style="margin-top:8px">Boundary Crossings</div>
    <div class="val">{boundary_count}</div>
    <div class="meta" style="margin-top:8px">VCS Operations</div>
    <div class="val">{vcs_ops}</div>
    <div class="meta" style="margin-top:8px">Running Services</div>
    <div class="val">{running_services}</div>
  </div>

  {tool_usage_panel}

  <div class="card">
    <h2>Journal</h2>
    <div class="meta">Path</div>
    <div class="mono">{journal_path}</div>
    <div class="meta" style="margin-top:8px">Entries</div>
    <div class="val">{journal_entries}</div>
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
<tbody>
{run_rows}
</tbody>
</table>
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
<meta http-equiv="refresh" content="10">
<title>Code Sandbox MCP — Containers</title>
{style}
</head>
<body>
<h1>Containers</h1>
<div class="subtitle">Live from Docker (managed containers) — auto-refresh 10s</div>
{nav}

{error}

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
<tbody>
{rows}
</tbody>
</table>

{sidecars}
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
  <div class="badge"><strong>Started:</strong> {started}</div>
  <div class="badge"><strong>Ended:</strong> {ended}</div>
  <div class="badge"><strong>Operations:</strong> {op_count}</div>
  <div class="badge"><strong>Boundary crossings:</strong> {boundary_count}</div>
  <div class="badge"><strong>Health:</strong> <span class="{health_cls}">{health}</span></div>
</div>
{phase_view}
<table>
<thead>
<tr><th>Time</th><th>Operation</th><th>Details</th></tr>
</thead>
<tbody>
{rows}
</tbody>
</table>
</body>
</html>"""


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
    cd_html = (
        f'<div class="metric-row">'
        f'<span class="metric-label">cd rate:</span> '
        f'<span class="metric-val" style="color:#ffa657">{usage["cd_rate_pct"]}%</span> '
        f'<span class="metric-note">({usage["cd_count"]} / {usage["exec_entry_count"]} exec entries)</span>'
        f'</div>'
    )

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
        return "—"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    return f"{seconds / 3600:.1f}h"


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


def _render_containers_page(
    failed_stop: str | None = None,
    now: str | None = None,
) -> str:
    """Render the ``/containers`` page from Docker's own view of the world.

    *failed_stop* is a stop that could not be carried out.  Both it and a
    Docker-level listing failure are errors, which is why they share one banner
    and both have their ``Error:`` prefix stripped -- the banner is never used
    for anything that isn't a failure.

    *now* is the classification timestamp (ISO-8601); defaults to the real
    clock so callers can inject a fixed value for deterministic tests.
    """
    containers, error = list_managed_containers()
    run_ids = get_run_id_per_container()
    envs = {
        env.get("container_id", ""): env
        for env in get_active_environments()
    }

    # Per-run health badges (Issue #775): aggregated from the journal, keyed
    # by run_id, so each row can badge the run its container is attached to.
    health_map = classify_all_runs(
        aggregate_run_phases(None, read_journal()),
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

    banner = error if error is not None else failed_stop
    error_html = ""
    if banner is not None:
        error_html = (
            f'<div class="card" style="margin-bottom:16px">'
            f'<span class="badge err">error</span> '
            f'{_escape(_strip_error_prefix(banner))}'
            f'</div>'
        )

    return _CONTAINERS_HTML.format(
        style=_STYLE,
        nav=_render_nav("containers"),
        error=error_html,
        rows=rows,
        sidecars=_render_sidecars(sidecars),
    )



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
        runs = get_runs()
        total_ops = 0
        boundary_count = 0
        vcs_ops = 0
        for r in runs:
            total_ops += r.get("operations", 0)
            boundary_count += r.get("boundary_crossings", 0)
            vcs_ops += r.get("vcs_operations", 0)

        journal_entries = 0
        jp = get_journal_path()
        try:
            with open(jp) as f:
                journal_entries = sum(1 for _ in f)
        except Exception:
            pass

        active_envs = get_active_environments()
        running_services = sum(
            len(env.get("services", [])) for env in active_envs
        )

        # Per-run health badges (Issue #775): one classification pass over
        # the whole aggregation state, then a lookup per row.
        health_map = classify_all_runs(
            aggregate_run_phases(None, read_journal()),
            _now_iso(),
        )

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

        # Parse tool usage time range from query string
        tool_from: str | None = None
        tool_to: str | None = None
        if "?" in self.path:
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            tool_from = qs.get("tool_from", [None])[0]
            tool_to = qs.get("tool_to", [None])[0]

        tool_usage_panel = _render_tool_usage_panel(tool_from, tool_to)

        html_content = _DASHBOARD_HTML.format(
            style=_STYLE,
            nav=_render_nav("home"),
            total_runs=len(runs),
            total_ops=total_ops,
            boundary_count=boundary_count,
            vcs_ops=vcs_ops,
            running_services=running_services,
            journal_path=str(get_journal_path()),
            journal_entries=journal_entries,
            run_rows="\n".join(run_rows_parts) if run_rows_parts else '<tr><td colspan="8" class="empty">No runs recorded</td></tr>',
            tool_usage_panel=tool_usage_panel,
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

        entries = read_journal()
        agg_state = aggregate_run_phases(None, entries)

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

        html_content = _render_insights_page(insights, period)
        self._send_html(html_content)

    def _serve_api_runs(self) -> None:
        runs = get_runs()
        self._send_json(runs)

    def _serve_api_journal(self) -> None:
        entries = read_journal(max_entries=500)
        self._send_json(entries)

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

        entries = read_journal(run_id=run_id)
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
                ec = e.get("exit_code", 0)
                ec_cls = "exit-ok" if ec == 0 else "exit-err"
                details = f'<span class="cmds">{_escape(cmds)}</span> <span class="{ec_cls}">exit={ec}</span>'
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
        agg_state = aggregate_run_phases(None, read_journal())
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
