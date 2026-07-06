"""Run-level replay trace output in HTML and JSON formats (§9).

Generates a self-contained HTML trace and a structured JSON trace
from journal entries for a specific run_id.  Useful for post-hoc
review of "why did it do that?".
"""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from code_sandbox_mcp.journal import read_journal

# ---------------------------------------------------------------------------
# Trace output directory
# ---------------------------------------------------------------------------

_TRACE_DIR: Path = Path.home() / ".code-sandbox-mcp" / "traces"

#: Max trace files to keep before cleaning old ones.
_TRACE_MAX_FILES: int = 100


def _ensure_trace_dir() -> None:
    _TRACE_DIR.mkdir(parents=True, exist_ok=True)


def _cleanup_old_traces() -> None:
    """Remove oldest trace files when the count exceeds limit.

    Keeps at most :data:`_TRACE_MAX_FILES` trace files, deleting
    the least recently modified ones first.  Only affects ``.html``
    and ``.json`` files in the trace directory.
    """
    if not _TRACE_DIR.exists():
        return
    files = sorted(
        [p for p in _TRACE_DIR.iterdir() if p.suffix in (".html", ".json") and p.is_file()],
        key=lambda p: p.stat().st_mtime,
    )
    while len(files) > _TRACE_MAX_FILES:
        f = files.pop(0)
        f.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# JSON trace
# ---------------------------------------------------------------------------


def generate_json_trace(run_id: str) -> str:
    """Generate a JSON trace for *run_id* and return the file path."""
    entries = read_journal(run_id=run_id)
    if not entries:
        return ""

    trace: dict[str, Any] = {
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

    _ensure_trace_dir()
    out_path = _TRACE_DIR / f"{run_id}.json"
    out_path.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
    _cleanup_old_traces()
    return str(out_path)


# ---------------------------------------------------------------------------
# HTML trace
# ---------------------------------------------------------------------------


_TEMPLATE: str = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Run Trace — {run_id}</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: system-ui, sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; }}
h1 {{ font-size: 18px; color: #58a6ff; margin-bottom: 16px; }}
.summary {{ display: flex; gap: 16px; margin-bottom: 20px; flex-wrap: wrap; }}
.badge {{ background: #21262d; border: 1px solid #30363d; border-radius: 6px; padding: 8px 14px; font-size: 13px; }}
.badge strong {{ color: #f0f6fc; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
th, td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid #21262d; }}
th {{ background: #161b22; color: #8b949e; font-weight: 600; }}
tr:hover {{ background: #161b22; }}
.op {{ font-weight: 600; }}
.op.init {{ color: #7ee787; }}
.op.exec {{ color: #a5d6ff; }}
.op.stop {{ color: #f97583; }}
.op.boundary {{ color: #ffa657; }}
.crossing {{ color: #ffa657; font-weight: 600; }}
.exit-ok {{ color: #7ee787; }}
.exit-err {{ color: #f97583; }}
.cmds {{ font-family: monospace; font-size: 12px; max-width: 400px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
</style>
</head>
<body>
<h1>Run Trace — {run_id}</h1>
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


def _escape(text: str) -> str:
    return html.escape(text, quote=True)


def _op_class(operation: str) -> str:
    mapping = {
        "initialize": "init",
        "exec": "exec",
        "stop": "stop",
        "boundary_crossing": "boundary",
    }
    return mapping.get(operation, "")


def generate_html_trace(run_id: str) -> str:
    """Generate an HTML trace page for *run_id* and return the file path."""
    entries = read_journal(run_id=run_id)
    if not entries:
        return ""

    rows_parts: list[str] = []
    for e in entries:
        op = e.get("operation", "unknown")
        cls = _op_class(op)
        details = ""

        if op == "initialize":
            details = f'image={_escape(e.get("image", ""))} net={e.get("allow_network","")}'
        elif op == "exec":
            cmds = " && ".join(e.get("commands", []))
            ec = e.get("exit_code", 0)
            ec_cls = "exit-ok" if ec == 0 else "exit-err"
            details = f'<span class="cmds">{_escape(cmds)}</span> <span class="{ec_cls}">exit={ec}</span>'
        elif op == "boundary_crossing":
            details = (_escape(e.get("sub_operation", "")) + " "
                       + _escape(e.get("details", "")))
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

    html_content = _TEMPLATE.format(
        run_id=run_id,
        started=started,
        ended=ended,
        op_count=len(entries),
        boundary_count=boundary_count,
        rows="\n".join(rows_parts),
    )

    _ensure_trace_dir()
    out_path = _TRACE_DIR / f"{run_id}.html"
    out_path.write_text(html_content, encoding="utf-8")
    _cleanup_old_traces()
    return str(out_path)


def get_trace_dir() -> str:
    """Return the directory where trace files are stored."""
    _ensure_trace_dir()
    return str(_TRACE_DIR)
