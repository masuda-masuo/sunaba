# code-sandbox-mcp

MCP server for Docker sandbox execution — AI-driven test, lint, type-check, and VCS workflows in disposable containers.

## Why sandbox?

AI coding agents that operate directly on the host filesystem carry maximum risk: a single `rm -rf ~` or `git push --force` can destroy your working environment, SSH keys, and git configuration. Recovery is painful and sometimes impossible.

This MCP routes all AI operations through **disposable Docker containers** with structural safety guarantees:

| Guarantee | Mechanism |
|-----------|-----------|
| AI operations never touch the host | All file ops, package installs, and test runs happen inside the container. If the AI breaks something — delete the container and move on. |
| No network by default | `allow_network=True` must be explicitly set. AI can't accidentally call external APIs, download payloads, or push to remotes. |
| Non-root execution | Container runs as unprivileged user `sandbox`. No `sudo`, no system package modification. |
| VCS token opt-in | `GITHUB_TOKEN` is injected only when `inject_vcs_token=True` is set. Even then, token values are masked in all output (`KEY=***`). |
| Audit trail | Every operation is recorded in an append-only journal. You can trace exactly what the AI did, after the fact. |

The value of this MCP is as much about **what the AI cannot do** as what it can.

For the full design rationale and decision-making principles, see [docs/design.md](docs/design.md).

### Reducing host permissions

A less obvious but equally important benefit: **this MCP lets you turn off broad host permissions in your AI client.**

Without a sandbox MCP, AI agents operate directly on the host via shell tools (`Bash`, `PowerShell`, etc.). Every file edit, git command, or config change triggers a permission prompt — and permission fatigue sets in fast. Users end up allowing everything just to keep work flowing, which means the AI effectively has unrestricted access to the host.

With this MCP, all real work happens inside the container. Host-level shell tools become unnecessary for the vast majority of tasks, so you can keep those permissions off by default. The result: the AI is structurally constrained, not just policy-constrained.

## Quick start

```bash
pip install git+https://github.com/masuda-masuo/code-sandbox-mcp
```

Minimal `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "code-sandbox-mcp": {
      "command": "python",
      "args": [
        "-m", "code_sandbox_mcp.launcher",
        "--pass-through-env", "GITHUB_TOKEN"
      ],
      "env": {
        "GITHUB_TOKEN": "github_pat_xxxx"
      }
    }
  }
}
```

> Use `which python` (Linux/macOS) or `(Get-Command python).Source` (Windows) to find the Python executable path if `"python"` alone doesn't work.

## Installation

```bash
# Install
pip install git+https://github.com/masuda-masuo/code-sandbox-mcp

# Update
pip install --force-reinstall git+https://github.com/masuda-masuo/code-sandbox-mcp

# Pin to a specific commit
pip install git+https://github.com/masuda-masuo/code-sandbox-mcp@<commit-hash>

# Uninstall
pip uninstall code-sandbox-mcp
```

Requirements: Python 3.10+, Docker.

## Configuration

### Transport: stdio vs SSE/HTTP

stdio has a ~60 second client timeout. Long operations (`docker pull`, `pip install`, large `copy_project`) should use SSE or HTTP transport:

```json
"args": [
  "-m", "code_sandbox_mcp.launcher",
  "--transport", "sse",
  "--host", "127.0.0.1",
  "--port", "8765",
  "--pass-through-env", "GITHUB_TOKEN"
]
```

| Transport | Timeout | Notes |
|-----------|---------|-------|
| `stdio` (default) | ~60s | Works with all clients |
| `sse` | None | Recommended for long operations |
| `http` | None | Standard HTTP |
| `streamable-http` | None | MCP spec Streamable HTTP |

For SSE/HTTP transports, the server binds to `127.0.0.1:8765` by default. The launcher manages the server process lifecycle but does not proxy stdio.

### `--pass-through-env`

Forward host environment variables into containers:

```json
"--pass-through-env", "GITHUB_TOKEN,SLACK_TOKEN"
```

Only explicitly listed variables are forwarded.

### Optional: terminal window for live logs

```json
"--terminal", "xterm"
```

When set, `sandbox_exec_background` opens a terminal window tailing container logs in real time. On Windows, use PowerShell (`powershell.exe`); on macOS use `osascript`; on Linux use `gnome-terminal` or `xterm`.

Use `--terminal-args` to pass custom arguments. `{container_id}` is substituted at runtime:

```json
"--terminal-args", "-NoExit -Command docker exec {container_id} tail -f /tmp/mcp.log"
```

### Optional: observability dashboard

```json
"--dashboard-port", "8766"
```

Starts a local read-only web dashboard at `http://127.0.0.1:8766` showing active containers, run history, pass/fail stats, and the approval queue.

### Optional: push notifications

```json
"--webhook-url", "https://hooks.example.com/notify",
"--failure-threshold", "5",
"--long-run-seconds", "300"
```

Sends OS desktop notifications (Linux) or webhook notifications on boundary-crossing operations, failure threshold exceeded, or long-running executions.

### In-place update (launcher mode)

Launcher mode (`-m code_sandbox_mcp.launcher`) supports in-place server updates without restarting Claude Desktop:

```
sandbox_update_start()    → returns job_id
sandbox_update_check()    → poll until "done"
```

The `--update-spec` flag controls the pip install source (default: `"."`).

> **Note:** The default `"."` only works when running from a local checkout that contains `pyproject.toml`.
> If you installed via `pip install git+https://...`, you must explicitly set `--update-spec`:
>
> ```json
> "args": ["-m", "code_sandbox_mcp.server", "--update-spec", "git+https://github.com/masuda-masuo/code-sandbox-mcp"]
> ```
>
> Without this, `sandbox_update_start` will fail with `Neither 'setup.py' nor 'pyproject.toml' found`.

## Available tools

### Lifecycle

| Tool | Description |
|------|-------------|
| `sandbox_initialize` | Start a container. Returns 12-char `container_id`. Supports `image`, `allow_network`, `inject_vcs_token`. |
| `sandbox_stop` | Stop and remove a container. |
| `run_container_and_exec` | One-shot: `initialize` → `exec` → `stop`. |

### Execution

| Tool | Description |
|------|-------------|
| `sandbox_exec` | Run commands synchronously. Supports `verbose` (`error_only`/`summary`/`full`), truncation, pagination (`offset`/`limit`). |
| `sandbox_exec_background` | Run commands with `nohup` in background. Returns `job_id`. Opens terminal window if `--terminal` is set. |
| `sandbox_exec_check` | Poll background job status. Returns `"running"`, stdout on success, or error on failure. |

### File operations

| Tool | Description |
|------|-------------|
| `write_file_sandbox` | **Primary edit path for AI.** Write/update files. Supports full overwrite, line-range replacement, append, and `old_str` replacement (uniqueness check + whitespace-flexible fallback). |
| `copy_project` | Copy a local directory into the container (tar archive streaming). |
| `copy_file` | Copy a single local file into the container. |

### Edit/Verify subsystem

| Tool | Description |
|------|-------------|
| `transform_file` | **Imperative edit path.** Edit a file by supplying Python `transform(text) -> str` that computes the new content (runs inside the container; returns a unified diff). Best for bulk / repetitive / structural / computed edits where `old_str` would need many calls and a diff would be huge. Single `code` string — no shell escaping. |
| `apply_patch` | **Deprecated for AI-authored edits.** Apply a unified diff to a file. LLM-written diffs almost always fail on `@@` header counts / context whitespace, and each failed retry costs a full round-trip — making it *more* expensive, not less. For AI editing use `write_file_sandbox` with `old_str` (the default edit path) or `transform_file`. Reserve `apply_patch` for **machine-generated** diffs (`git diff` / `diff -u`). |
| `read_file_range` | Read `limit` lines starting at `offset`. Returns JSON with pagination metadata. |
| `lint_in_container` | Run linter on a file (`.py` → ruff/pylint, `.js/.ts/.jsx/.tsx` → eslint). |
| `type_check_in_container` | Run type checker on a file (`.py` → mypy/pyright, `.ts/.tsx` → tsc). |

### In-place update

| Tool | Description |
|------|-------------|
| `sandbox_update_start` | Start `pip install --force-reinstall` in background. Opens terminal window if `--terminal` configured. |
| `sandbox_update_check` | Poll update job status. |

### Observability

| Tool | Description |
|------|-------------|
| `sandbox_read_journal` | Read the append-only execution journal. Filter by `run_id`, limit by `max_entries`. |
| `sandbox_trace` | Generate HTML or JSON replay trace for a specific `run_id`. |
| `sandbox_list_runs` | List all runs recorded in the journal. |
| `sandbox_journal_path` | Return path to `~/.code-sandbox-mcp/journal.log`. |
| `sandbox_trace_dir` | Return path to `~/.code-sandbox-mcp/traces/`. |

## Sandbox image

The default image is a purpose-built sandbox image pushed to GHCR. It bundles all tools needed for AI-driven workflows:

| Category | Tool | Purpose |
|----------|------|---------|
| Text search | `ripgrep` (`rg`) | Fast regex search |
| Structural search | `ast-grep` (`sg`) | AST-based code search |
| Text replace | `sd` | Find-and-replace |
| File search | `fd` | Fast `find` alternative |
| Symbols | `universal-ctags` | Code indexing |
| Lint | `ruff` | Python linting |
| Type check | `pyright` | Python type checking |
| Security | `semgrep` | Static analysis |
| VCS | `git`, `gh` | Version control, GitHub CLI |
| Package install | `uv` | Fast pip alternative |
| JSON | `jq` | JSON processing |

The image is built from `docker/Dockerfile.sandbox` and automatically published to GHCR via CI. To use a custom image, pull it first and pass it explicitly:

```
sandbox_initialize(image="my-image@sha256:...")
```

A minimal variant (`docker/Dockerfile.sandbox.minimal`) with git + python + pytest only is also available for lightweight use.

## Launcher architecture

```
Claude Desktop
    └─ launcher  ← lightweight, stays alive
           └─ server  ← actual MCP server (child process)
```

When `sandbox_update_start()` succeeds, the server exits with exit code 42. The launcher detects this and restarts the server automatically — no Claude Desktop restart needed.

In SSE/HTTP mode, the launcher only manages the server process lifecycle. The client connects directly to the server's TCP port.

## VCS token safety

Token injection into containers is **opt-in**. By default, no VCS credentials are passed to containers.

- Set `inject_vcs_token=True` in `sandbox_initialize` or `run_container_and_exec` to inject `GITHUB_TOKEN` / `GH_TOKEN`.
- Use `allow_network=True` only when containers need network access (e.g., for `git clone` or `gh` commands).
- All output is automatically sanitized: token values are masked as `KEY=***` in stdout/stderr.

This follows the principle of least privilege — containers that don't need VCS access don't get tokens.

## Observability

The server maintains an append-only execution journal at `~/.code-sandbox-mcp/journal.log`. Every container lifecycle event (initialize, exec, stop) and boundary-crossing operation is recorded with timestamps and run IDs.

| Component | Description |
|-----------|-------------|
| **Journal** | Append-only log of all operations. `tail -f` for real-time monitoring. |
| **Trace** | HTML or JSON replay for any `run_id`. Post-hoc review of "why did it do that?" |
| **Dashboard** | Local web UI at `http://127.0.0.1:<dashboard-port>`. Read-only, auto-refreshing. |
| **Notifications** | OS desktop notifications or webhook callbacks for boundary-crossing events and failures. |

## Workflow example

```
# One-shot: initialize, run commands, auto-stop
run_container_and_exec(
    image="python@sha256:...",
    commands=[
        "git clone https://github.com/user/repo.git /app",
        "cd /app && pip install .[test] -q",
        "cd /app && pytest tests/ -v"
    ],
    allow_network=True,
    inject_vcs_token=True
)

# Or step-by-step for longer sessions
sandbox_initialize(image="python@sha256:...", allow_network=True)
  → container_id

sandbox_exec_background(container_id, [
    "apt-get update -qq && apt-get install -y -qq git",
    "git clone https://github.com/user/repo.git /app",
    "cd /app && pip install .[test] -q",
    "cd /app && pytest tests/ -v"
])
  → job_id

sandbox_exec_check(container_id, job_id)
sandbox_stop(container_id)
```

## Human-in-the-loop (HITL) — beyond terminal output

Existing AI coding tools (Claude Code, Open Code / Codex CLI, Copilot) display test results as transient terminal text. Once the output scrolls past, it's gone — there is no structured record of what happened, when, and why.

code-sandbox-mcp shifts the human from **active watching** to **passive monitoring**:

| Capability | Claude Code / Open Code | code-sandbox-mcp |
|------------|--------------------------|-------------------|
| Test output | Terminal text, ephemeral | Structured journal with per-operation timeline |
| Pass/fail visibility | Scroll through raw output | Color-coded badges (green/red) at a glance |
| "Why did it fail?" | Re-run to see | Click a run → HTML/JSON trace with full context |
| Cross-run comparison | Manual, by memory | Side-by-side in dashboard |
| Audit trail | None (session-scoped) | Append-only journal, survives restarts |
| Boundary crossing ops | Invisible | Explicitly tracked, approval queue |
| Human attention model | Must watch terminal | Check dashboard anytime, catch up in seconds |

The dashboard (`--dashboard-port 8766`) runs on localhost, auto-refreshes every 10 seconds, and requires no external services. When a test fails, the trace shows exactly which assertion broke, on which line — no re-run needed.

> This is the HITL layer that design.md §8-9 describes: the human's final control shifts from pre-execution approval to **post-hoc audit**, and the dashboard makes that audit practical rather than theoretical.

## Development

### Setting up a local development environment

```bash
git clone https://github.com/masuda-masuo/code-sandbox-mcp.git
cd code-sandbox-mcp
pip install -e .[test]
```

`pip install -e .` (editable install) ensures that your source tree under `src/` is used at runtime and in tests. **Do not use a plain `pip install .`** alongside an editable install — having both a regular and an editable installation of the same package causes non-deterministic import resolution where Python may load the stale `site-packages` copy instead of your working tree.

If you previously installed the package without `-e` (e.g. via `pip install git+https://...`), remove it first:

```bash
pip uninstall code-sandbox-mcp   # repeat until "not installed"
pip install -e .[test]
```

Verify that imports resolve to the source tree:

```bash
python -c "import inspect, code_sandbox_mcp.server; print(inspect.getfile(code_sandbox_mcp.server))"
# Expected: .../code-sandbox-mcp/src/code_sandbox_mcp/server.py
```

## Known limitations

- **Job state is in-memory**: Background job results are lost on server restart. Job IDs become invalid after `sandbox_update_start()`.
- **Job dictionary grows unbounded**: Completed job results accumulate in memory. Not an issue for typical short-lived sessions.
- **SSE transport requires client support**: Not all MCP clients support SSE-based servers.
- **Background jobs are lost on server restart**: Use `run_container_and_exec` for critical one-shot operations.

## License

MIT
