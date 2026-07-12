# Observability & Dashboard

Sunaba provides multiple observability layers to move the human operator from active terminal watching to passive post-hoc auditing. This document describes the local web dashboard, execution journal, replay traces, and push notifications.

---

## 1. Local Web Dashboard

Starts automatically by default alongside the MCP server. Binds to `127.0.0.1:8751` and auto-refreshes every 10 seconds.

```
+-------------------------------------------------------------------+
| sunaba Dashboard                                                  |
+-------------------------------------------------------------------+
|  [Active Containers]                                              |
|  - issue-123 (python:3.12, age: 2h, idle: 15m)  [Stop]            |
|                                                                   |
|  [Run History]                                                    |
|  - run_abc123: pytest tests/  (PASSED, 4.2s)    [View Trace]      |
|  - run_def456: pytest tests/  (FAILED, 1.2s)    [View Trace]      |
+-------------------------------------------------------------------+
```

### Dashboard CLI Flags

| Flag | Default | Description |
|---|---|---|
| `--dashboard-port` | `8751` | Port to run the dashboard. Set to `0` to disable the dashboard entirely. |
| `--dashboard-host` | `127.0.0.1` | Bind address. Binds to loopback by default. |

> [!TIP]
> **WSL2 tip**: When running inside WSL2, the dashboard binds to localhost inside the Linux environment. It is accessible from your Windows host browser at `http://localhost:8751` via WSL2's automatic localhost forwarding.

---

## 2. Append-Only Execution Journal

Every container lifecycle event, command execution, and boundary-crossing operation is recorded chronologically in `~/.sunaba/journal.log`.

*   **Real-time inspection**: Run `tail -f ~/.sunaba/journal.log` on the host to monitor operations as they happen.
*   **Rotation policy**: When the active log file reaches 100 MB, it is rotated to `journal.log.1`. The maximum disk space used by the journal is bounded to roughly 200 MB (one active file + one backup). Log readers automatically merge and read both files in chronological order.
*   **Tool access**: If environment variable `SUNABA_OBSERVABILITY_TOOLS=1` is set, you can query this log via the `sandbox_read_journal` MCP tool.

---

## 3. Replay Traces

For every execution run, Sunaba saves detailed HTML/JSON replay traces inside `~/.sunaba/traces/`.

*   **Audit utility**: If a test run fails, you can click on the run in the dashboard or open the trace file directly to inspect the exact assertion error and stdout/stderr output. Re-running the command inside the container is not required to debug the failure.
*   **Cleanup policy**: Sunaba retains at most 100 trace files. When a new run generates a trace, the oldest trace is automatically pruned.

---

## 4. Real-time Push Notifications

Sunaba can send notifications on critical events. Bounded by thresholds to prevent alert fatigue.

*   **OS Desktop Notifications**: Bounces notifications to the host desktop (`notify-send` on Linux, `osascript` on macOS, or PowerShell toast on Windows).
*   **Webhook Integration**: Dispatches a JSON HTTP POST body to a custom endpoint configured via `--webhook-url`.

### Alert Triggers
Notifications are triggered by three classes of events:
1.  **Boundary-crossing operations**: When `publish` or `sandbox_issue_write` is invoked to write back to GitHub.
2.  **Failure Threshold Exceeded**: When consecutive command or test failures exceed a threshold (configurable via `--failure-threshold`, default is `5`).
3.  **Long-running Executions**: When a command runs longer than a specified duration (configurable via `--long-run-seconds`, default is `300` seconds).
