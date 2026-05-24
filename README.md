# code-sandbox-mcp

MCP server for Docker sandbox execution with `--pass-through-env` support.

Inspired by [Automata-Labs-team/code-sandbox-mcp](https://github.com/Automata-Labs-team/code-sandbox-mcp).

## Why this exists

The original Automata-Labs version does not support passing host environment variables into containers. This implementation adds `--pass-through-env` so credentials stored in `claude_desktop_config.json` (e.g. `GITHUB_TOKEN`) are forwarded to the container — following the [MCP security best practice](https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices) of keeping secrets out of AI context.

## Requirements

- Python 3.10+
- Docker Desktop (Windows/Mac) or Docker Engine (Linux)
- `pip`

## Installation

Install directly with pip:

```powershell
pip install git+https://github.com/masuda-masuo/code-sandbox-mcp
```

Then find the installed executable path:

```powershell
where.exe code-sandbox-mcp   # Windows
which code-sandbox-mcp       # Mac/Linux
```

### Uninstall

```powershell
pip uninstall code-sandbox-mcp
```

### Update to latest

```powershell
pip install --force-reinstall git+https://github.com/masuda-masuo/code-sandbox-mcp
```

### Update to a specific commit

```powershell
pip install --force-reinstall git+https://github.com/masuda-masuo/code-sandbox-mcp@<commit-hash>
```

After updating, restart Claude Desktop to load the new version.

### claude_desktop_config.json

```json
{
  "mcpServers": {
    "code-sandbox-mcp": {
      "command": "C:\\Users\\User\\AppData\\Local\\Programs\\Python\\Python312\\Scripts\\code-sandbox-mcp.exe",
      "args": [
        "--pass-through-env", "GITHUB_TOKEN"
      ],
      "env": {
        "GITHUB_TOKEN": "github_pat_xxxx"
      }
    }
  }
}
```

> **Note**: `uvx --from git+https://...` is not recommended. uvx does not cache git-sourced packages and attempts to fetch from GitHub on every Claude Desktop startup, causing connection failures. Use `pip install` and specify the full executable path instead.

`--pass-through-env` accepts a comma-separated list of variable names:

```
"--pass-through-env", "GITHUB_TOKEN,SLACK_TOKEN"
```

Only the listed variables are forwarded. Variables not listed are never injected into containers.

### Version pinning (recommended for stability)

To avoid picking up unintended changes, pin to a specific commit:

```powershell
pip install git+https://github.com/masuda-masuo/code-sandbox-mcp@<commit-hash>
```

### Exec timeout (optional)

Default command execution timeout is 300 seconds. Override with `--exec-timeout`:

```json
"args": ["--pass-through-env", "GITHUB_TOKEN", "--exec-timeout", "600"]
```

## Available tools

| Tool | Description |
|------|-------------|
| `sandbox_initialize` | Start a container, returns `container_id` |
| `sandbox_exec` | Run commands inside the container (synchronous) |
| `sandbox_exec_background` | Start commands in background, returns `job_id` |
| `sandbox_exec_check` | Poll background job status and retrieve output |
| `sandbox_stop` | Stop and remove the container |
| `write_file_sandbox` | Write a file into the container |
| `copy_project` | Copy a local directory into the container |
| `copy_file` | Copy a single local file into the container |

## When to use background execution

MCP clients (including Claude Desktop) have a request timeout of ~60 seconds. Commands that take longer — such as `apt-get install`, `pip install`, or `pytest` on large projects — will cause a timeout error if run with `sandbox_exec`.

**Use `sandbox_exec_background` + `sandbox_exec_check` for any heavy operation.**

```
sandbox_initialize(image="python:3.12-slim-bookworm")
  → container_id

# Heavy operations: use background execution
sandbox_exec_background(container_id, [
  "apt-get update -qq && apt-get install -y -qq git",
  "git clone https://user:$GITHUB_TOKEN@github.com/org/repo.git /app",
  "cd /app && pip install .[test] -q",
  "cd /app && pytest tests/ -v"
])
  → job_id

# Poll until done
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
sandbox_initialize(image="python:3.12-slim-bookworm")
  → container_id

sandbox_exec_background(container_id, [
  "apt-get update -qq && apt-get install -y -qq git",
  "git clone https://masuda-masuo:$GITHUB_TOKEN@github.com/masuda-masuo/mstu_bot.git /app",
  "cd /app && pip install .[test] -q",
  "cd /app && pytest tests/ -v"
])
  → job_id

# Poll every 30 seconds until "Status: done"
sandbox_exec_check(container_id, job_id)

sandbox_stop(container_id)
```

## Known limitations

- **Job dictionary grows unbounded**: Completed job results are kept in memory indefinitely for the lifetime of the server process. In typical development use (short-lived server sessions) this is not a problem, but long-running server instances will accumulate memory over time.
- **Background jobs are lost on server restart**: Job state is in-process memory only. If the MCP server restarts, all job IDs become invalid.
- **MCP is synchronous by design**: The background execution pattern is an application-level workaround for the MCP 60-second timeout, not a native async feature of the protocol.

## License

MIT
