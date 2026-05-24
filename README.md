# code-sandbox-mcp

MCP server for Docker sandbox execution with `--pass-through-env` support.

Inspired by [Automata-Labs-team/code-sandbox-mcp](https://github.com/Automata-Labs-team/code-sandbox-mcp).

## Why this exists

The original Automata-Labs version does not support passing host environment variables into containers. This implementation adds `--pass-through-env` so credentials stored in `claude_desktop_config.json` (e.g. `GITHUB_TOKEN`) are forwarded to the container — following the [MCP security best practice](https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices) of keeping secrets out of AI context.

## Requirements

- Python 3.10+
- Docker Desktop (Windows/Mac) or Docker Engine (Linux)
- `uv` (recommended) or `pip`

## Installation

### claude_desktop_config.json

```json
{
  "mcpServers": {
    "code-sandbox-mcp": {
      "command": "uvx",
      "args": [
        "--from", "git+https://github.com/masuda-masuo/code-sandbox-mcp",
        "code-sandbox-mcp",
        "--pass-through-env", "GITHUB_TOKEN"
      ],
      "env": {
        "GITHUB_TOKEN": "github_pat_xxxx"
      }
    }
  }
}
```

`--pass-through-env` accepts a comma-separated list of variable names:

```
"--pass-through-env", "GITHUB_TOKEN,SLACK_TOKEN"
```

Only the listed variables are forwarded. Variables not listed are never injected into containers.

### Version pinning (recommended for stability)

To avoid picking up unintended changes, pin to a specific tag:

```json
"--from", "git+https://github.com/masuda-masuo/code-sandbox-mcp@v1.0.0"
```

## Available tools

| Tool | Description |
|------|-------------|
| `sandbox_initialize` | Start a container, returns `container_id` |
| `sandbox_exec` | Run commands inside the container |
| `sandbox_stop` | Stop and remove the container |
| `write_file_sandbox` | Write a file into the container |
| `copy_project` | Copy a local directory into the container |
| `copy_file` | Copy a single local file into the container |

## Typical workflow (pytest example)

```
sandbox_initialize(image="python:3.12-slim-bookworm")
  → container_id

sandbox_exec(container_id, [
  "apt-get update -qq && apt-get install -y -qq git",
  "git clone https://masuda-masuo:$GITHUB_TOKEN@github.com/masuda-masuo/mstu_bot.git /app",
  "cd /app && pip install .[test] -q",
  "pytest tests/ -v"
])

sandbox_stop(container_id)
```

## License

MIT
