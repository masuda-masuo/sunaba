# code-sandbox-mcp

**Most AI coding tools optimize for humans. code-sandbox-mcp optimizes for frontier LLMs.**

> Less context. Less trust. More structure.

An MCP server that runs an AI's test → verify → publish workflow inside disposable Docker containers. It assumes the model is already capable, so it spends its effort elsewhere: stripping away the context bloat, the broad host trust, and the raw-log noise that frontier models don't need — and shouldn't have.

## What's different

Most sandboxing tools are built around a human watching a terminal. This one is built around a model reasoning over a small, structured context window:

- **The LLM is assumed competent.** No sprawling toolset to hand-hold it — a small set of first-class verbs (search, edit, verify, publish) plus an image full of CLIs it already knows.
- **The context is never polluted.** The payload — issue bodies, source files, diffs — stays inside the container. The model carries only `run_id`s, handles, and structured summaries.
- **Output is structured, not raw.** A green run is one line; a failure is `{test, error, file, line}`. No 5000-line logs scrolling through the context window.
- **Trust is structural, not policy.** The host is cut off by construction, so you grant the AI *less* standing access — not more careful prompts.

The result is an MCP whose value is as much about **what it withholds from the model** — context, trust, noise — as what it gives it.

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

### Reducing host permissions

A less obvious but equally important benefit: **this MCP lets you turn off broad host permissions in your AI client.**

Without a sandbox MCP, AI agents operate directly on the host via shell tools (`Bash`, `PowerShell`, etc.). Every file edit, git command, or config change triggers a permission prompt — and permission fatigue sets in fast. Users end up allowing everything just to keep work flowing, which means the AI effectively has unrestricted access to the host.

With this MCP, all real work happens inside the container. Host-level shell tools become unnecessary for the vast majority of tasks, so you can keep those permissions off by default. The result: the AI is structurally constrained, not just policy-constrained.

## Design philosophy

This is not "an MCP that drives Docker." It is **a foundation for an AI to run test → verify → publish workflows safely, with minimal context**. Adding features is never the goal; the goal is to spend fewer tokens, preserve reasoning accuracy, and keep the human in final control. Four principles drive every decision.

### 1. Security and convenience are not a tradeoff — the sandbox dissolves it

Normally, security and convenience pull against each other: widen permissions and you gain convenience, narrow them and you lose it. This MCP places a disposable container as a *middle layer* so you don't have to choose:

- **To the AI**, it feels like working locally — same operations, same speed.
- **To the host**, the AI is structurally cut off — you can turn host shell permissions off entirely.

The corollary is a strict stance: **"if it can't be done inside the sandbox, that's a bug in the tooling."** Reaching for direct local work to dodge a limitation just hides a design failure. Every feature is judged against this goal.

### 2. AI-first: return structure and diffs, not raw logs

The most expensive thing an AI coding loop does is re-read output. So the contract is **don't show everything — return structure, not raw data, and reveal detail progressively.**

- **Structured results, not 5000-line logs.** A passing test run is a one-liner: `{status: ok, passed: 120, duration: 4.2s}`. A failure is `{test, error, file, line}` — the exact assertion and location, no scrollback.
- **State lives on the server; the LLM holds a handle.** Every result is keyed by a `run_id`. "Show me the rest of the log" or "re-run only what failed" cost a handle, not a giant re-submission. Large artifacts (coverage, generated files) come back as sized resource handles, never inlined.
- **Diffs, not full text.** Across an iteration loop the tools return only what changed since last time (`sandbox_exec_diff`, `rerun_failed`), fold duplicate failures into `×N`, and serve cached results when inputs are unchanged.
- **Denoise before returning.** ANSI color, timestamps, progress bars, and library/framework stack frames are stripped so only the user's code remains.

> The escape hatch is always present: defaults are diffs and summaries, but the full output is *always* retrievable by handle via `offset`/`limit`.

### 3. The line to defend is the sandbox boundary — not "dangerous" commands

Most sandboxing tools try to classify each command as safe or dangerous. That depends on self-reporting and can't be structurally enforced. This MCP draws the line elsewhere: **the question is not "is this command dangerous?" but "does this operation leave the sandbox?"**

- **Inside the container, nothing is gated.** It's disposable — whatever happens in there just gets deleted with the container.
- **Boundary-crossing operations are gated structurally.** Network access, host mounts, persistent-volume deletion, and external VCS writes (`git push`, PR creation) require an explicit two-step approval token; there is no way to perform them without it.
- **What can't be perfectly gated is caught after the fact.** An append-only journal records every operation, so the real safety net is *post-hoc auditability*, not pre-execution approval. The human's control shifts from "watch and approve everything" to "audit anything, anytime."

This is the three-lock model: **network off by default · non-root enforced · VCS token opt-in.** Half the value of this MCP is the *guarantee of what the AI cannot do*.

### 4. The payload never passes through the LLM

In the `issue → fix → verify → publish` flow, the issue body, the source files, and the diff all stay inside the container. The LLM only ever carries `run_id`s, handles, and structured summaries — never the raw payload. This keeps context small and keeps sensitive content (tokens, large diffs) out of the model's window. Tokens injected for VCS access are opt-in per container and automatically masked (`KEY=***`) in all output.

For the full rationale and the decision principles behind each tool, see [docs/design.md](docs/design.md).

## Core ideas at a glance

```
                 Your AI client
                       │
             (host shell tools OFF)
                       │
        ┌──────────────────────────────┐
        │        code-sandbox-mcp       │
        │   structured, minimal-context │
        │          control plane        │
        │  ──────────────────────────── │
        │   • container lifecycle       │
        │   • structured outputs        │
        │   • boundary approval gate    │
        │   • append-only journal       │
        └──────────────────────────────┘
                       │
              Disposable container
                       │
            payload stays in here:
          source · diffs · tests · git
```

The model drives the control plane with small, structured messages. The heavy data — repos, diffs, logs — lives and dies inside the container. The only thing that ever crosses the boundary on purpose is a `publish`.

## Typical workflow

You don't need the 30-tool reference to understand what this is for. A normal session is one pipeline:

```
clone_repo            # pull the repo into a fresh container
    ↓
write_file_sandbox    # edit in place (or transform_file for bulk/computed edits)
    ↓
verify_in_container   # lint + type-check gate, then tests → structured result
    ↓
checkpoint            # cheap in-container save point (no token, no gate)
    ↓
publish               # the one boundary-crossing exit: push + optional PR
```

The issue body, the source, and the diff never leave the container; the model only ever sees `run_id`s and structured summaries. Everything else — backgrounding, search, caching, observability — exists to make this loop cheaper to run and easier to audit. The full per-tool reference is [further down](#available-tools).

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
        "-m", "code_sandbox_mcp.server"
      ]
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
  "-m", "code_sandbox_mcp.server",
  "--transport", "sse",
  "--host", "127.0.0.1",
  "--port", "8765"
]
```

| Transport | Timeout | Notes |
|-----------|---------|-------|
| `stdio` (default) | ~60s | Works with all clients |
| `sse` | None | Recommended for long operations |
| `http` | None | Standard HTTP |
| `streamable-http` | None | MCP spec Streamable HTTP |

For SSE/HTTP transports, the server binds to `127.0.0.1:8765` by default.

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

## Available tools

This is the full reference. You almost never touch most of it directly — the common path is the five-step [Typical workflow](#typical-workflow) above. The rest exists to make that loop cheaper (caching, diffs, backgrounding) and auditable (journal, trace, dashboard).

### Lifecycle

| Tool | Description |
|------|-------------|
| `sandbox_initialize` | Start a container. Returns 12-char `container_id`. Supports `image`, `allow_network`, `inject_vcs_token`. |
| `sandbox_stop` | Stop and remove a container. |
| `run_container_and_exec` | One-shot: `initialize` → `exec` → `stop`. |
| `run_test_environment` | Start a Compose-like multi-service test environment with health checks. |
| `wait_for_condition` | Wait for TCP port open, HTTP 2xx, or log pattern match (replaces `sleep 30`). |
| `stop_test_environment` | Stop and remove a test environment started by `run_test_environment`. |

### Execution

| Tool | Description |
|------|-------------|
| `sandbox_exec` | Run commands synchronously. Supports `verbose` (`error_only`/`summary`/`full`), truncation, pagination (`offset`/`limit`). |
| `sandbox_exec_background` | Run commands with `nohup` in background. Returns `job_id`. |
| `sandbox_exec_check` | Poll background job status. Returns `"running"`, stdout on success, or error on failure. |
| `sandbox_exec_diff` | Execute commands and return only the diff from the cached result. |
| `rerun_failed` | Re-run failed commands from a previous `run_id`, returning only the diff. |

### File operations

| Tool | Description |
|------|-------------|
| `write_file_sandbox` | **Primary edit path for AI.** Write/update files. Supports full overwrite, line-range replacement, append, and `old_str` replacement (uniqueness check + whitespace-flexible fallback). |
| `transform_file` | **Imperative edit path.** Edit a file by supplying Python `transform(text) -> str` that computes the new content (runs inside the container; returns a unified diff). Best for bulk / repetitive / structural / computed edits. |
| `read_file_range` | Read `limit` lines starting at `offset`. Returns JSON with pagination metadata. |
| `list_files` | List files inside the container using `find`. Returns JSON array of file paths. |
| `copy_project` | Copy a local directory into the container (tar archive streaming). |
| `copy_file` | Copy a single local file into the container. |

### Edit/Verify subsystem

| Tool | Description |
|------|-------------|
| `search_in_container` | Search for text patterns across files. Lexical (ripgrep) or structural (ast-grep) mode. |
| `lint_in_container` | Run linter on a file (`.py` → ruff/pylint, `.js/.ts/.jsx/.tsx` → eslint). Pass `fix=True` to apply `ruff check --fix` / `eslint --fix` autofixes and return the remaining findings. |
| `type_check_in_container` | Run type checker on a file (`.py` → pyright, `.ts/.tsx` → tsc). |
| `verify_in_container` | **Pre-publish test gate.** Run pytest with optional filter, then auto-full-suite. Returns diff summary. |

### Observability

| Tool | Description |
|------|-------------|
| `sandbox_read_journal` | Read the append-only execution journal. Filter by `run_id`, limit by `max_entries`. |
| `sandbox_trace` | Generate HTML or JSON replay trace for a specific `run_id`. |
| `sandbox_list_runs` | List all runs recorded in the journal. |
| `sandbox_journal_path` | Return path to `~/.code-sandbox-mcp/journal.log`. |
| `sandbox_trace_dir` | Return path to `~/.code-sandbox-mcp/traces/`. |

### VCS / Versioning

| Tool | Description |
|------|-------------|
| `clone_repo` | Clone a Git repository inside the container using `gh repo clone`. |
| `issue_view` | Read a GitHub issue and save its body to a file inside the container. |
| `checkpoint` | Local Git checkpoint (commit only, no push). Use frequently during edit loops. |
| `checkpoint_list` | List unpushed local checkpoints. |
| `checkpoint_restore` | Restore working tree to a previous checkpoint (`git reset --hard`). |
| `publish` | Stage, commit, push, and optionally create a PR. Two-step flow: dry_run → token. |

### Sandbox management

| Tool | Description |
|------|-------------|
| `sandbox_approval_status` | List all pending approval tokens for boundary-crossing operations. |
| `sandbox_approve` | Approve a pending boundary-crossing operation. |
| `sandbox_reject` | Reject a pending boundary-crossing operation. |
| `sandbox_cache_invalidate` | Invalidate result cache entries. |
| `sandbox_cache_stats` | Return result cache statistics. |

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
| VCS | `git`, `gh` | Version control, GitHub CLI |
| Package install | `uv` | Fast pip alternative |
| JSON | `jq` | JSON processing |

The images are built from the split `docker/Dockerfile.{base,python,go}` and automatically published to GHCR via CI. When `sandbox_initialize` is called without an explicit `image`, the project's language is detected (from a Shiori pre-clone or the GitHub repo root) and the matching variant is chosen:

| Detected | Image |
|----------|-------|
| Python (`pyproject.toml`, `setup.py`, `requirements*.txt`, ...) | `sandbox:python` (base + python + js) |
| Go (`go.mod`) | `sandbox:go` (base + go + js) |
| JS only / unknown / unsupported / py+go polyglot | `sandbox:base` (neutral: node + VCS + search, no language toolchain) |

No language is hardcoded as the default. Unknown or unsupported projects fall back to the neutral `sandbox:base` (init never blocks); a notice in the result explains the fallback. To override detection, pass an image explicitly:

```
sandbox_initialize(image="my-image@sha256:...")
```

A minimal variant (`docker/Dockerfile.sandbox.minimal`) with git + python + pytest only is also available for lightweight use.

## Recommended: mcp-launcher for credential management

For production use, run `code-sandbox-mcp` behind [mcp-launcher](https://github.com/masuda-masuo/mcp-launcher) to keep GitHub tokens in the OS keystore instead of plaintext config files:

```
AI Tool (Claude Desktop / etc.)
    └─ mcp-launcher  ← OS keystore, transparent MCP session restart
           └─ code-sandbox-mcp  ← actual MCP server (child process)
```

mcp-launcher eliminates PATs from `claude_desktop_config.json`, automatically rotates GitHub App installation tokens, and transparently restarts the MCP session without losing state.

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

- **Job state is in-memory**: Background job results are lost on server restart.
- **Job dictionary grows unbounded**: Completed job results accumulate in memory. Not an issue for typical short-lived sessions.
- **SSE transport requires client support**: Not all MCP clients support SSE-based servers.
- **Background jobs are lost on server restart**: Use `run_container_and_exec` for critical one-shot operations.

## License

MIT
