# code-sandbox-mcp

MCP server for Docker sandbox execution with `--pass-through-env` support.

Inspired by [Automata-Labs-team/code-sandbox-mcp](https://github.com/Automata-Labs-team/code-sandbox-mcp).

## Why this exists

### 1. Pass host credentials into containers securely

The original Automata-Labs version does not support passing host environment variables into containers. This implementation adds `--pass-through-env` so credentials stored in `claude_desktop_config.json` (e.g. `GITHUB_TOKEN`) are forwarded to the container — following the [MCP security best practice](https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices) of keeping secrets out of AI context.

### 2. Token-efficient workflows via git clone

Instead of having the AI read source files and write them back into the container one by one, the AI simply runs `git clone` inside the container. The entire codebase is fetched directly from GitHub without ever passing through the AI's context window — saving a significant amount of tokens on large projects.

Only the results (e.g. `pytest` output) are returned to the AI, keeping both input and output tokens minimal.

### 3. Reproducible, transparent test environments

Built-in AI sandboxes are opaque: the OS, installed packages, and runtime versions are unknown and uncontrollable. With this MCP, the Docker image is specified explicitly by the user:

```
sandbox_initialize(image="python:3.11-slim-bookworm")
```

Any image on Docker Hub can be used, including custom images that replicate a production environment exactly. The AI runs tests in the same environment every time — no surprises from mismatched dependencies or hidden system packages.

## Requirements

- Python 3.10+
- Docker Desktop (Windows/Mac) or Docker Engine (Linux)
- `pip`

## Installation

Install directly with pip:

```powershell
pip install git+https://github.com/masuda-masuo/code-sandbox-mcp
```

### Uninstall

```powershell
pip uninstall code-sandbox-mcp
```

### Update to latest

```powershell
pip install --force-reinstall git+https://github.com/masuda-masuo/code-sandbox-mcp
```

Or ask Claude directly (launcher mode only):

```
sandbox_update_start()
sandbox_update_check(job_id="...")  # repeat until "Status: done"
```

### Update to a specific commit

```powershell
pip install --force-reinstall git+https://github.com/masuda-masuo/code-sandbox-mcp@<commit-hash>
```

### claude_desktop_config.json

#### Recommended: launcher mode (supports in-place updates)

Use `(Get-Command python).Source` (Windows PowerShell) or `which python` (Mac/Linux) to find the Python executable path.

```json
{
  "mcpServers": {
    "code-sandbox-mcp": {
      "command": "C:\\Users\\User\\AppData\\Local\\Programs\\Python\\Python312\\python.exe",
      "args": [
        "-m", "code_sandbox_mcp.launcher",
        "--update-spec", "git+https://github.com/masuda-masuo/code-sandbox-mcp",
        "--pass-through-env", "GITHUB_TOKEN"
      ],
      "env": {
        "GITHUB_TOKEN": "github_pat_xxxx"
      }
    }
  }
}
```

In launcher mode, `sandbox_update_start()` / `sandbox_update_check()` allow Claude to update the server without restarting Claude Desktop.

#### Simple mode (no in-place update)

```json
{
  "mcpServers": {
    "code-sandbox-mcp": {
      "command": "C:\\Users\\User\\AppData\\Local\\Programs\\Python\\Python312\\python.exe",
      "args": [
        "-m", "code_sandbox_mcp.server",
        "--pass-through-env", "GITHUB_TOKEN"
      ],
      "env": {
        "GITHUB_TOKEN": "github_pat_xxxx"
      }
    }
  }
}
```

> **Note**: `uvx --from git+https://...` is not recommended. uvx does not cache git-sourced packages and attempts to fetch from GitHub on every Claude Desktop startup, causing connection failures. Use `pip install` and specify the Python executable path instead.

`--pass-through-env` accepts a comma-separated list of variable names:

```json
"--pass-through-env", "GITHUB_TOKEN,SLACK_TOKEN"
```

Only the listed variables are forwarded. Variables not listed are never injected into containers.

### Optional terminal window (live logs)

Add `--terminal` to open a PowerShell/terminal window that tails container logs in real time:

```json
"args": [
  "-m", "code_sandbox_mcp.launcher",
  "--update-spec", "git+https://github.com/masuda-masuo/code-sandbox-mcp",
  "--pass-through-env", "GITHUB_TOKEN",
  "--terminal", "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"
]
```

### Version pinning (recommended for stability)

```powershell
pip install git+https://github.com/masuda-masuo/code-sandbox-mcp@<commit-hash>
```

### Exec timeout (optional)

Default command execution timeout is 300 seconds. Override with `--exec-timeout`:

```json
"args": ["--pass-through-env", "GITHUB_TOKEN", "--exec-timeout", "600"]
```

## Launcher architecture

```
Claude Desktop
    └─ launcher  ← Claude holds this (lightweight, stays alive)
           └─ server  ← actual MCP server (child process)
```

The launcher proxies stdio between Claude Desktop and the server. When `sandbox_update_start()` succeeds, the server exits with a restart signal (exit code 42) and the launcher restarts it automatically — without requiring a Claude Desktop restart.

## In-place update (launcher mode only)

```
sandbox_update_start()
  → job_id

# Ask Claude to notify you when done, or poll manually:
sandbox_update_check(job_id="...")  # repeat until "Status: done"
```

The `--update-spec` flag...
Ask Claude to notify you when done, or poll manually:
sandbox_update_check(job_id="...")  # repeat until "Status: done"

The `--update-spec` flag controls the pip install source (default: `git+https://github.com/masuda-masuo/code-sandbox-mcp`).

## Terminal auto-open (optional)

When `--terminal` is set, a terminal window opens automatically every time `sandbox_exec_background` is called, tailing `/tmp/mcp.log` inside the container so you can watch command output in real time.

### How it works

- Each command's stdout/stderr is tee'd to `/tmp/mcp.log` inside the container.
- A new terminal window runs `docker exec <container_id> tail -f /tmp/mcp.log`.
- On Windows, the terminal is launched via `cmd /c start` so it runs as an independent process — it stays open even after the MCP server exits.
- When the container stops, `tail -f` exits and a banner is printed. The window stays open (`-NoExit` + `Read-Host`) so you can review the output before closing manually.

### Windows configuration

```json
{
  "mcpServers": {
    "code-sandbox-mcp": {
      "command": "<output of where.exe code-sandbox-mcp>",
      "args": [
        "--pass-through-env", "GITHUB_TOKEN",
        "--terminal", "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"
      ],
      "env": {
        "GITHUB_TOKEN": "github_pat_xxxx"
      }
    }
  }
}
```

### macOS configuration (experimental)

```json
"args": [
  "--pass-through-env", "GITHUB_TOKEN",
  "--terminal", "/usr/bin/osascript"
]
```

### Linux configuration (experimental)

```json
"args": [
  "--pass-through-env", "GITHUB_TOKEN",
  "--terminal", "/usr/bin/gnome-terminal"
]
```

> **Note**: The `--terminal` option has been verified on **Windows only**. macOS and Linux support is implemented but untested. Behavior may differ depending on the terminal emulator and system configuration.

### Custom terminal args

Use `--terminal-args` to pass custom arguments to the terminal. `{container_id}` is substituted at runtime:

```json
"--terminal-args", "-NoExit -Command docker exec {container_id} tail -f /tmp/mcp.log"
```

## Docker images

`sandbox_initialize` requires a Docker image to be available locally. If the image is not present, the tool will return an error. Pull the image manually before use:

```powershell
docker pull python:3.12-slim-bookworm
```

### Default image

| Image | Description |
|-------|-------------|
| `python:3.12-slim-bookworm` | Default. Debian 12 slim, Python 3.12. Suitable for most Python projects. |

### Other options

| Image | Use case |
|-------|----------|
| `python:3.11-slim-bookworm` | Projects requiring Python 3.11 |
| `python:3.12-bookworm` | Full Debian image (larger, includes more system packages) |
| `ubuntu:24.04` | General-purpose Linux environment |
| `node:20-slim` | Node.js projects |

Any image available on [Docker Hub](https://hub.docker.com) can be used. Pull it first with `docker pull <image>`.

## Available tools

| Tool | Description |
|------|-------------|
| `sandbox_initialize` | Start a container, returns `container_id` |
| `sandbox_exec` | Run commands inside the container (synchronous) |
| `sandbox_exec_background` | Start commands in background, returns `job_id`. Opens a terminal window automatically if `--terminal` is set. |
| `sandbox_exec_check` | Poll background job status and retrieve output |
| `sandbox_stop` | Stop and remove the container |
| `write_file_sandbox` | Write a file into the container |
| `copy_project` | Copy a local directory into the container |
| `copy_file` | Copy a single local file into the container |
| `sandbox_update_start` | Start an in-place server update, returns `job_id` (launcher mode only) |
| `sandbox_update_check` | Poll update status |

## When to use background execution

MCP clients (including Claude Desktop) have a request timeout of ~60 seconds. Commands that take longer — such as `apt-get install`, `pip install`, or `pytest` on large projects — will cause a timeout error if run with `sandbox_exec`.

**Use `sandbox_exec_background` + `sandbox_exec_check` for any heavy operation.**

```
sandbox_initialize(image="python:3.12-slim-bookworm")
  → container_id

sandbox_exec_background(container_id, [
  "apt-get update -qq && apt-get install -y -qq git",
  "git clone https://user:$GITHUB_TOKEN@github.com/org/repo.git /app",
  "cd /app && pip install .[test] -q",
  "cd /app && pytest tests/ -v"
])
  → job_id

# Poll until done (PowerShell terminal window shows live logs if --terminal is set)
sandbox_exec_check(container_id, job_id)  # repeat until "Status: done"

sandbox_stop(container_id)
```

**Use `sandbox_exec` only for quick commands** (expected to finish well within 60 seconds):

```
sandbox_exec(container_id, ["echo $GITHUB_TOKEN | head -c 10"])
sandbox_exec(container_id, ["ls /app"])
```

## Typical workflow (pytest example)

```
# 1. Pull the image first (one-time setup)
docker pull python:3.12-slim-bookworm

# 2. Run the workflow
sandbox_initialize(image="python:3.12-slim-bookworm")
  → container_id

sandbox_exec_background(container_id, [
  "apt-get update -qq && apt-get install -y -qq git",
  "git clone https://masuda-masuo:$GITHUB_TOKEN@github.com/masuda-masuo/mstu_bot.git /app",
  "cd /app && pip install .[test] -q",
  "cd /app && pytest tests/ -v"
])
  → job_id

# PowerShell shows live output if --terminal is configured
# Tell Claude when done; Claude will call sandbox_exec_check to retrieve results
sandbox_exec_check(container_id, job_id)

sandbox_stop(container_id)
```

## Known limitations

- **Job dictionary grows unbounded**: Completed job results are kept in memory indefinitely for the lifetime of the server process. In typical development use (short-lived server sessions) this is not a problem, but long-running server instances will accumulate memory over time.
- **Background jobs are lost on server restart**: Job state is in-process memory only. If the MCP server restarts (e.g. after `sandbox_update_start()`), all job IDs become invalid.
- **MCP is synchronous by design**: The background execution pattern is an application-level workaround for the MCP 60-second timeout, not a native async feature of the protocol.

## License

MIT
