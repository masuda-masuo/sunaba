"""Pure rendering helpers for the local dashboard (§9).

Split verbatim out of :mod:`sunaba.dashboard`.  Everything here turns data it
was handed into HTML: no journal reads, no container inspection, no process
state, no locks.

That restriction is load-bearing rather than stylistic.  The test suite
patches the data-gathering names on the ``sunaba.dashboard`` namespace, and a
function that reached for one of them from *this* module would resolve it
here instead and sail straight past the patch -- the tests would keep passing
while testing nothing.  So the gathering stays in ``sunaba.dashboard`` and
calls into these renderers, never the other way round;
``tests/test_dashboard_split.py`` enforces it.
"""
from __future__ import annotations

import html as _html
import json
import re
from datetime import datetime, timezone
from typing import Any

from sunaba.dashboard_templates import _INSIGHTS_HTML, _LIVE_SCRIPT, _NAV, _STYLE
from sunaba.health import HEALTH_ORDER
from sunaba.journal import get_tool_usage


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
            f'{rtrips} verify-fail \u2192 retry loop(s)'
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


def _strip_error_prefix(message: str) -> str:
    """Drop the ``Error:`` prefix the tool layer adds for its LLM caller.

    The page says "error" in a badge next to it, so the word is redundant here.
    """
    return message.removeprefix("Error: ")


def _shorten_image_digest(image: str) -> str:
    """Shorten 64-char sha256 digests in *image* to 4 hex chars + ellipsis."""
    s = re.sub(r"sha256:([0-9a-fA-F]{4})[0-9a-fA-F]+", r"sha256:\1" + "\u2026", image)
    s = re.sub(r"\b([0-9a-fA-F]{4})[0-9a-fA-F]{60}\b", r"\1" + "\u2026", s)
    return s


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
    err = d.get("per_tool_error_rate") or {
        "by_tool": [],
        "total_calls": 0,
        "total_failures": 0,
        "recovery_distribution": {},
    }
    err_rows: list[str] = []
    for tool in err.get("by_tool", []):
        rate_color = "#7ee787" if tool["failure_rate"] == 0 else (
            "#ffa657" if tool["failure_rate"] < 0.2 else "#f97583"
        )
        extra_note = ""
        if tool["operation"] == "exec" and "expected_failures" in tool:
            extra_note = f' <span style="font-size:10px;color:#8b949e">(of which expected non-zero: {tool["expected_failures"]})</span>'
        sources = tool.get("sources", {})
        title_attr = ""
        if len(sources) > 1:
            sources_str = " + ".join(f"{k} {v:,}" for k, v in sources.items())
            title_text = f'{tool["operation"]} ▸ {sources_str}'
            title_attr = f' title="{_escape(title_text)}"'
        err_rows.append(
            f'<tr>'
            f'<td class="mono"{title_attr}>{_escape(tool["operation"])}</td>'
            f'<td>{tool["calls"]}</td>'
            f'<td>{tool["failures"]}{extra_note}</td>'
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
        f'({err.get("total_calls", 0)} calls, {err.get("total_failures", 0)} failures)'
        f'</span></h2>'
        f'{err_table}'
        f'{recovery_html}'
        f'</div>'
    )

    # ── Metric 2: First-verify failure rate by repo ──
    fvr = d.get("first_verify_failure_by_repo") or {
        "by_repo": [],
        "overall": {"total_runs_with_verify": 0, "total_first_failed": 0, "failure_rate": 0.0},
    }
    fv = d.get("first_verify_failure_by_image") or {
        "by_image": [],
        "overall": {"total_runs_with_verify": 0, "total_first_failed": 0, "failure_rate": 0.0},
    }
    overall = fvr.get("overall") or fv.get("overall") or {
        "total_runs_with_verify": 0, "total_first_failed": 0, "failure_rate": 0.0
    }

    fvr_rows: list[str] = []
    for r in fvr.get("by_repo", []):
        rate_color = "#7ee787" if r["failure_rate"] == 0 else (
            "#ffa657" if r["failure_rate"] < 0.3 else "#f97583"
        )
        fvr_rows.append(
            f'<tr>'
            f'<td class="mono">{_escape(r["repo"])}</td>'
            f'<td>{r["total_runs"]}</td>'
            f'<td>{r["first_verify_failed"]}</td>'
            f'<td style="color:{rate_color}">{r["failure_rate"]:.1%}</td>'
            f'</tr>'
        )
    fvr_table = (
        f'<table><thead><tr>'
        f'<th>Repo</th><th>Runs</th><th>Failed</th><th>Rate</th>'
        f'</tr></thead><tbody>{"".join(fvr_rows)}</tbody></table>'
        if fvr_rows else '<div class="empty">No runs with verify data</div>'
    )

    fv_rows: list[str] = []
    for img in fv.get("by_image", []):
        rate_color = "#7ee787" if img["failure_rate"] == 0 else (
            "#ffa657" if img["failure_rate"] < 0.3 else "#f97583"
        )
        short_img = _shorten_image_digest(img["image"])
        fv_rows.append(
            f'<tr>'
            f'<td class="mono">{_escape(short_img)}</td>'
            f'<td>{img["total_runs"]}</td>'
            f'<td>{img["first_verify_failed"]}</td>'
            f'<td style="color:{rate_color}">{img["failure_rate"]:.1%}</td>'
            f'</tr>'
        )
    by_image_details = ""
    if fv_rows:
        fv_img_table = (
            f'<table><thead><tr>'
            f'<th>Image</th><th>Runs</th><th>Failed</th><th>Rate</th>'
            f'</tr></thead><tbody>{"".join(fv_rows)}</tbody></table>'
        )
        by_image_details = (
            f'<details><summary>By container image</summary>'
            f'{fv_img_table}'
            f'</details>'
        )

    first_verify_panel = (
        f'<div class="card">'
        f'<h2>2. First-Verify Failure by Repo '
        f'<span style="font-size:11px;color:#8b949e;font-weight:400">'
        f'(overall: {overall.get("total_first_failed", 0)}/{overall.get("total_runs_with_verify", 0)} = {overall.get("failure_rate", 0.0):.1%})'
        f'</span></h2>'
        f'{fvr_table}'
        f'{by_image_details}'
        f'</div>'
    )

    # ── Metric 3: Roundtrip distribution ──
    rd = d.get("roundtrip_distribution") or {
        "histogram": [],
        "mean_roundtrips": 0.0,
        "total_runs": 0,
    }
    max_count = max((item["count"] for item in rd.get("histogram", [])), default=1)
    rt_bars = ""
    for item in rd.get("histogram", []):
        rt_bars += _render_bar(item["count"], max_count, item["bucket"], color="#d2a8ff")
    rt_mean_html = (
        f'<div style="font-size:12px;color:#f0f6fc;margin-top:8px">'
        f'Mean: {rd.get("mean_roundtrips", 0.0)} roundtrips/run ({rd.get("total_runs", 0)} runs)'
        f'</div>'
    )
    roundtrip_panel = (
        f'<div class="card">'
        f'<h2>3. Verify retry loops</h2>'
        f'{rt_bars}'
        f'{rt_mean_html}'
        f'</div>'
    )

    # ── Metric 4: Low-frequency tools ──
    low_freq = d.get("low_frequency_tools")
    if low_freq is None and "unused_tools" in d:
        low_freq = d.get("unused_tools")
    if low_freq is None:
        low_freq = []
    low_freq_rows: list[str] = []
    for item in low_freq:
        calls_td = f'<td>{item["calls"]}</td>' if "calls" in item else ''
        low_freq_rows.append(
            f'<tr><td class="mono">{_escape(item["operation"])}</td>'
            f'{calls_td}'
            f'<td class="dim">{_escape(item.get("reason", ""))}</td></tr>'
        )
    has_calls = any("calls" in item for item in low_freq) or not low_freq
    calls_th = '<th>Calls</th>' if has_calls else ''
    low_freq_table = (
        f'<table><thead><tr><th>Operation</th>{calls_th}<th>Reason</th></tr></thead>'
        f'<tbody>{"".join(low_freq_rows)}</tbody></table>'
        if low_freq_rows else '<div class="empty">No low-frequency tools</div>'
    )
    unused_panel = (
        f'<div class="card">'
        f'<h2>4. Low-Frequency Tools (≤5 calls) '
        f'<span style="font-size:11px;color:#8b949e;font-weight:400">'
        f'({len(low_freq)} candidate{"" if len(low_freq) == 1 else "s"})'
        f'</span></h2>'
        f'{low_freq_table}'
        f'</div>'
    )

    # ── Metric 5: Run duration & op-count distributions ──
    rdists = d.get("run_distributions") or {}

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
    initd = d.get("initialize_duration_distribution") or {
        "stats": {"count": 0, "min": 0, "max": 0, "mean": 0, "median": 0},
        "histogram": [],
        "abandoned": 0,
        "in_flight": 0,
        "total_runs": 0,
    }
    init_stats = initd.get("stats") or {"count": 0, "min": 0, "max": 0, "mean": 0, "median": 0}
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
    busy = d.get("busy_refusal_counts") or {"by_pool": [], "total": 0}
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

    # ── Metric 8: Verify failure reasons ──
    vfr = d.get("verify_failure_reasons", {"total_failed": 0, "by_kind": {}})
    by_kind = vfr.get("by_kind", {})
    total_failed = vfr.get("total_failed", 0)
    sorted_kinds = sorted(by_kind.items(), key=lambda x: (-x[1], x[0]))
    max_count = max((c for _, c in sorted_kinds), default=1)
    vfr_bars = ""
    for kind, count in sorted_kinds:
        vfr_bars += _render_bar(count, max_count, kind, color="#f97583")
    vfr_table = (
        vfr_bars
        if vfr_bars
        else '<div class="empty">No verify failure reasons recorded</div>'
    )
    verify_reasons_panel = (
        f'<div class="card">'
        f'<h2>Verify failure reasons (window) '
        f'<span style="font-size:11px;color:#8b949e;font-weight:400">'
        f'({total_failed} total)'
        f'</span></h2>'
        f'{vfr_table}'
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
        verify_reasons_panel=verify_reasons_panel,
        run_dist_panel=run_dist_panel,
        init_duration_panel=init_duration_panel,
        busy_refusals_panel=busy_refusals_panel,
    )
