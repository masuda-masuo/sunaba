"""HTML/CSS/JS templates for the local dashboard (§9).

Split verbatim out of :mod:`sunaba.dashboard`, which had grown past 2,400
lines.  This module holds *only* string constants -- no imports at all, no
functions, no I/O -- so an edit to the markup cannot reach server behaviour,
and the server module stays readable.

Nothing here is patched by the tests: the names they patch live on
``sunaba.dashboard`` and have to stay there, which is why this module imports
nothing.  ``tests/test_dashboard_split.py`` enforces both halves of that.
"""

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
