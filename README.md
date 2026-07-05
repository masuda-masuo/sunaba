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
| VCS tokens stay host-side | The container never receives a `GITHUB_TOKEN`. Reads authenticate through the [egress proxy](#vcs-token-safety)'s read-authorization window; `publish` / `sandbox_issue_write` resolve the token host-side. Token values are masked in all output (`KEY=***`). |
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
- **Boundary-crossing operations are gated structurally.** Network access, host mounts, persistent-volume deletion, and external VCS writes (`git push`, PR creation) go through dedicated tools; egress to GitHub is confined by an egress proxy (allowlisted repos, short-lived authorized push/read windows), so an unauthorized network write is structurally impossible — not merely discouraged. The human gate is your MCP client's own tool-approval prompt, not a bespoke in-band token.
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
        │   • egress proxy gate         │
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

Starts a local read-only web dashboard at `http://127.0.0.1:8766` showing active containers, run history, and pass/fail stats.

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
| `sandbox_initialize` | Start a container. Returns 12-char `container_id`. Supports `image`, `allow_network`. |
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
| `publish` | Stage, commit, push, and optionally create a PR (one-shot). |
| `sandbox_issue_write` | Create a GitHub issue or comment on one, host-side (one-shot, #414). |

### Sandbox management

| Tool | Description |
|------|-------------|
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

## Deployment & credential management

For production use, keep GitHub credentials in the **OS keystore** instead of plaintext config files and let the server mint short-lived tokens on demand. The security model below is the same on every platform — only the keystore backend, the unlock UX, and how the server process is launched differ.

> This section covers **host-side** credential resolution (how the server authenticates to GitHub). It is complementary to [VCS token safety](#vcs-token-safety) below, which covers why the *container* never receives a token, regardless of platform.

### The shared security model: short-lived GitHub App tokens

The recommended model uses [mcp-launcher](https://github.com/masuda-masuo/mcp-launcher)'s `mcp-token` broker:

- **Secrets live in the OS keystore, never in config files.** The GitHub App's `APP_ID`, `PRIVATE_KEY`, and `INSTALLATION_ID` are registered once with `mcp-token register github <KEY> <value>`.
- **Tokens are minted on demand and short-lived.** `mcp-token github` mints a fresh installation token (cached ~55 min; GitHub expiry 1 h). No long-lived token sits in a config file or environment.
- **The private key is out of reach.** A prompt-injection attack can only touch what the model sees — MCP tool responses — never the keystore. Worst case, a leaked token expires within an hour; the key that mints tokens is never exposed.
- **The host resolves the token; the container never holds it.** The resolved token is used host-side by the egress proxy's read/push windows and by `publish` / `sandbox_issue_write` (see [VCS token safety](#vcs-token-safety)).

The host resolves the token from one of three orthogonal sources (`token_broker.py`, design.md §11.2):

| Source | Host env var | How it works | Role |
|--------|-------------|--------------|------|
| Static token | `GITHUB_TOKEN` | Used verbatim | Minimal setup |
| Explicit command | `GITHUB_TOKEN_COMMAND` | Runs the command (e.g. an `mcp-token` binary path); stdout becomes the token. Takes priority. | Manual management |
| Vendored broker | `GITHUB_TOKEN_BROKER_SERVICE` | Resolves/downloads the pinned `mcp-token` binary (SHA-256 verified, cached) and runs `mcp-token <service>` | Auto-managed (recommended) |

> **Bootstrap note:** the broker binary lives in a private repo, so the very first download needs a token available (e.g. during setup, or a session still launched via mcp-launcher). After that the checksum-verified binary is cached and every later run reuses it. For a fully tokenless daemon, pre-provision the binary and point `GITHUB_TOKEN_BROKER_BIN` at it.

### Per-platform deployment

**Windows — mcp-launcher (stdio)**

Run `code-sandbox-mcp` behind mcp-launcher as a child process; the launcher proxies stdio and resolves the token internally, so no `GITHUB_TOKEN_*` env var is needed.

```
AI Tool (Claude Desktop / etc.)
    └─ mcp-launcher  ← Windows Credential Manager, transparent MCP session restart
           └─ code-sandbox-mcp  ← actual MCP server (child process)
```

- **Keystore:** Windows Credential Manager (DPAPI).
- **Unlock:** automatic at Windows login — no extra step.
- mcp-launcher eliminates PATs from `claude_desktop_config.json`, rotates the GitHub App installation token, and restarts the MCP session without losing state.

**WSL2 — systemd + streamable-HTTP**

Useful for sharing one server across clients (e.g. opencode + Claude Desktop), dropping the Windows-only mcp-launcher dependency, and avoiding stdio's ~60 s timeout.

```
Claude Desktop ─ mcp-remote ┐
                            ├─ code-sandbox-mcp (WSL2, streamable-http @ 127.0.0.1:8765/mcp)
opencode ───────────────────┘
```

Run the server as a systemd **user** service and point the token resolver at the broker:

```ini
# ~/.config/systemd/user/code-sandbox-mcp.service (excerpt)
[Service]
ExecStart=/path/to/venv/bin/python -m code_sandbox_mcp.server \
    --transport streamable-http --host 127.0.0.1 --port 8765
Environment="GITHUB_TOKEN_BROKER_SERVICE=code-sandbox-mcp"
# Required so the service can reach GNOME Keyring:
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
```

Clients connect via `mcp-remote`:

```json
{ "mcpServers": { "code-sandbox-mcp": {
  "command": "npx",
  "args": ["-y", "mcp-remote", "http://127.0.0.1:8765/mcp"]
}}}
```

- **Keystore:** GNOME Keyring (libsecret).
- **Unlock:** one password prompt on first access after WSL boots; stays unlocked for the session. (An empty-password keyring auto-unlocks but is then readable by any host process — a security trade-off.)
- **Gotchas:**
  - `Environment=` values containing spaces (e.g. a `GITHUB_TOKEN_COMMAND` with a service argument) **must be quoted**, or the argument is truncated.
  - Without `DBUS_SESSION_BUS_ADDRESS`, the service can't reach the keyring.
  - `mcp-token list` may fail under WSL (Go's godbus can't reach the D-Bus secret service); use `secret-tool search --all service mcp-launcher` instead.

**Linux (native) — systemd + HTTP**

Same as WSL2, but on a desktop-session Linux the keyring unlocks automatically with the session — no first-access password prompt.

- **Keystore:** libsecret / gnome-keyring / kwallet (needs `libsecret-1-0` + `gnome-keyring`).
- **Token resolution:** same broker as WSL2 (`GITHUB_TOKEN_BROKER_SERVICE` → `mcp-token`).
- **Transport:** `http` or `sse`; connect directly or via `mcp-remote`.

### Platform differences at a glance

| | Windows | WSL2 | Linux (native) |
|---|---|---|---|
| Server launch | mcp-launcher (stdio proxy, child process) | systemd user service | systemd user service |
| Transport | stdio / SSE / HTTP | streamable-HTTP (Claude Desktop via mcp-remote) | HTTP / SSE |
| Keystore | Windows Credential Manager (DPAPI) | GNOME Keyring (libsecret) | libsecret / gnome-keyring / kwallet |
| Token resolution | managed by launcher | `GITHUB_TOKEN_BROKER_SERVICE` → mcp-token | `GITHUB_TOKEN_BROKER_SERVICE` → mcp-token |
| Keyring unlock | automatic at Windows login | one prompt per WSL boot | automatic with desktop session |
| Main gotcha | Smart App Control may block the binary | quoting · `DBUS_SESSION_BUS_ADDRESS` · `secret-tool` fallback | keyring daemon must be running |

## VCS token safety

The sandbox container **never receives a VCS token.** There is no opt-in flag: credentials stay host-side, and the egress proxy is the structural guard that keeps them there. Use `allow_network=True` only when containers actually need network access.

- **Reads** (`clone_repo`, `sandbox_initialize(clone_repo=...)`, `pr=N`) authenticate at the network layer: when a repo needs credentials, a host-resolved token is handed to the proxy for a short read-authorization window, so even a *private* clone or PR checkout works without a token ever entering the container.
- **Pushes / PRs** go through `publish`: it resolves a token host-side and hands it to the proxy for a short authorized push window (push), or calls the GitHub API directly from the host (PR creation).
- **Issue / comment writes** go through `sandbox_issue_write`, which calls the GitHub REST API host-side.
- All output is automatically sanitized: any token value is masked as `KEY=***` in stdout/stderr.

This follows the principle of least privilege — the container's own `git`/`gh` stay unauthenticated, so a stray in-container `git push` has no credential to leak. The proxy must be configured with a host-resolvable token (broker / `GITHUB_TOKEN`) for the read and push windows to authenticate.

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
    allow_network=True
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
| Boundary crossing ops | Invisible | Explicitly tracked in the journal |
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