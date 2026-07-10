"""Local web dashboard for observability (§9).

Serves a read-mostly, auto-refreshing HTML dashboard on localhost
that shows running containers, run history, pass/fail counts,
resource usage.

Uses Python's built-in ``http.server`` — no external dependencies.
"""
from __future__ import annotations

import html as _html
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from sunaba.journal import (
    get_active_environments,
    get_journal_path,
    get_runs,
    get_tool_usage,
    read_journal,
)

# ---------------------------------------------------------------------------
# HTML template pages
# ---------------------------------------------------------------------------

_DASHBOARD_HTML: str = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="10">
<title>Code Sandbox MCP — Dashboard</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: system-ui, sans-serif; background: #0d1117; color: #c9d1d9; padding: 24px; }}
h1 {{ font-size: 20px; color: #58a6ff; margin-bottom: 8px; }}
.subtitle {{ color: #8b949e; font-size: 13px; margin-bottom: 24px; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 16px; margin-bottom: 24px; }}
.card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; }}
.card h2 {{ font-size: 14px; color: #58a6ff; margin-bottom: 12px; border-bottom: 1px solid #21262d; padding-bottom: 8px; }}
.card .meta {{ font-size: 12px; color: #8b949e; margin-bottom: 4px; }}
.card .val {{ font-size: 24px; font-weight: 600; color: #f0f6fc; }}
.badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }}
.badge.ok {{ background: #1b3820; color: #7ee787; }}
.badge.err {{ background: #381620; color: #f97583; }}
.badge.boundary {{ background: #382a10; color: #ffa657; }}
.badge.svc-starting {{ background: #382a10; color: #ffa657; }}
.badge.svc-ready {{ background: #1b3820; color: #7ee787; }}
table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
th, td {{ padding: 6px 10px; text-align: left; border-bottom: 1px solid #21262d; }}
th {{ color: #8b949e; font-weight: 600; }}
th.sortable {{ cursor: pointer; user-select: none; }}
th.sortable:hover {{ color: #58a6ff; }}
.pass {{ color: #7ee787; }}
.fail {{ color: #f97583; }}
.mono {{ font-family: monospace; font-size: 11px; }}
button {{ background: #21262d; border: 1px solid #30363d; color: #c9d1d9; padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 12px; }}
button:hover {{ opacity: 0.8; }}
.empty {{ color: #484f58; font-style: italic; padding: 12px 0; }}
.bar-wrap {{ display: flex; align-items: center; gap: 6px; margin: 1px 0; font-size: 11px; }}
.bar-label {{ width: 90px; text-align: right; color: #8b949e; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.bar-track {{ flex: 1; background: #21262d; border-radius: 3px; height: 14px; }}
.bar-fill {{ display: block; height: 100%; border-radius: 3px; background: #58a6ff; }}
.bar-num {{ width: 50px; text-align: right; color: #f0f6fc; font-family: monospace; }}
.filter-form {{ display: flex; gap: 8px; align-items: center; margin-bottom: 10px; }}
.filter-form input {{ background: #0d1117; border: 1px solid #30363d; color: #c9d1d9; padding: 3px 6px; font-size: 11px; border-radius: 4px; }}
.filter-form button {{ background: #21262d; border: 1px solid #30363d; color: #c9d1d9; padding: 3px 10px; font-size: 11px; border-radius: 4px; cursor: pointer; }}
.metric-row {{ margin-bottom: 6px; }}
.metric-label {{ color: #f0f6fc; }}
.metric-val {{ font-size: 18px; font-weight: 600; }}
.metric-note {{ font-size: 11px; color: #484f58; }}
details {{ font-size: 10px; color: #484f58; margin-top: 8px; }}
.section-header {{ font-size: 11px; color: #8b949e; margin-bottom: 2px; margin-top: 8px; }}
</style>
</head>
<body>
<h1>Code Sandbox MCP</h1>
<div class="subtitle">Observability Dashboard — localhost only — auto-refresh 10s</div>

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

{active_environments}

<h2 style="font-size: 16px; color: #8b949e; margin-bottom: 12px;">Recent Runs</h2>
<table>
<thead>
<tr>
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
</div>
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


def _escape(text: str) -> str:
    """HTML-escape *text* for safe embedding in attribute values and text content.

    Uses ``html.escape(quote=True)`` which handles ``&``, ``<``, ``>``,
    ``"``, and ``'`` — sufficient for ``title`` and ``value`` attributes.
    Not suitable for ``href``, ``style``, ``on*``, or raw URL contexts.
    """
    return _html.escape(text, quote=True)


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


def _render_active_environments() -> str:
    """Render active test environments section."""
    environments = get_active_environments()
    if not environments:
        return ""

    rows: list[str] = []
    for env in environments:
        cid = _escape(env.get("container_id", ""))
        status = _escape(env.get("environment_status", "unknown"))
        status_cls = "svc-ready" if status == "ready" else "svc-starting"
        services = env.get("services", [])
        svc_names = ", ".join(s.get("name", "?") for s in services)
        rows.append(f"""<div class="card" style="margin-bottom:8px">
    <h2>Environment <span class="mono">{cid}</span> <span class="badge {status_cls}">{status}</span></h2>
    <div class="meta">Services: {_escape(svc_names)}</div>
</div>""")

    return f"""<div class="grid">
  <div class="card" style="grid-column: span 2;">
    <h2>Active Environments</h2>
    {"".join(rows)}
  </div>
</div>"""


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
        elif path == "/api/runs":
            self._serve_api_runs()
        elif path == "/api/journal":
            self._serve_api_journal()
        elif path == "/api/tool-usage":
            self._serve_api_tool_usage()
        elif path.startswith("/trace/"):
            self._serve_trace(path)
        else:
            self.send_error(404)

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

        run_rows_parts: list[str] = []
        for r in runs[:20]:  # show last 20 runs
            status = r.get("status", "running")
            status_cls = "err" if status == "running" else "ok"
            image_short = r.get("image", "unknown")
            if "@sha256:" in image_short:
                image_short = image_short.split("@sha256:")[0] + "@sha256:..."
            run_rows_parts.append(_RUN_ROW.format(
                run_id=r["run_id"],
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

        active_section = _render_active_environments()

        html_content = _DASHBOARD_HTML.format(
            total_runs=len(runs),
            total_ops=total_ops,
            boundary_count=boundary_count,
            vcs_ops=vcs_ops,
            running_services=running_services,
            journal_path=str(get_journal_path()),
            journal_entries=journal_entries,
            run_rows="\n".join(run_rows_parts) if run_rows_parts else '<tr><td colspan="7" class="empty">No runs recorded</td></tr>',
            active_environments=active_section,
            tool_usage_panel=tool_usage_panel,
        )
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

        html_content = _TRACE_HTML.format(
            run_id=run_id,
            started=started,
            ended=ended,
            op_count=len(entries),
            boundary_count=boundary_count,
            rows="\n".join(rows_parts),
        )
        self._send_html(html_content)


# ---------------------------------------------------------------------------
# Server manager
# ---------------------------------------------------------------------------


_dashboard_server: HTTPServer | None = None
_dashboard_thread: threading.Thread | None = None
_dashboard_host: str = "127.0.0.1"
_dashboard_port: int = 8751


def start_dashboard(host: str = "127.0.0.1", port: int = 8751) -> str:
    """Start the web dashboard on *host*:*port* in a background thread.

    When *port* is 0, the OS assigns a free ephemeral port.
    Use :func:`get_dashboard_url` to retrieve the actual bound address.

    Returns a status message.
    """
    global _dashboard_server, _dashboard_thread, _dashboard_host, _dashboard_port

    if _dashboard_server is not None:
        return f"Dashboard already running on http://{_dashboard_host}:{_dashboard_port}"

    _dashboard_host = host
    _dashboard_server = HTTPServer((host, port), _DashboardHandler)
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
