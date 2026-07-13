# sunaba

**Most AI coding tools optimize for humans. sunaba optimizes for frontier LLMs.**

> Less context. Less trust. More structure.

An MCP server that runs an AI's test → verify → publish workflow inside disposable Docker containers. It assumes the model is already capable, so it spends its effort elsewhere: stripping away the context bloat, the broad host trust, and the raw-log noise that frontier models don't need — and shouldn't have.

---

## What's different

Most sandboxing tools are built around a human watching a terminal. This one is built around a model reasoning over a small, structured context window:

*   **The LLM is assumed competent.** No sprawling toolset to hand-hold it — a small set of first-class verbs (search, edit, verify, publish) plus an image full of CLIs it already knows.
*   **The context is never polluted.** The payload — issue bodies, source files, diffs — stays inside the container. The model carries only `run_id`s, handles, and structured summaries.
*   **Output is structured, not raw.** A green run is one line; a failure is `{test, error, file, line}`. No 5000-line logs scrolling through the context window.
*   **Trust is structural, not policy.** The host is cut off by construction, so you grant the AI *less* standing access — not more careful prompts.

The result is an MCP whose value is as much about **what it withholds from the model** — context, trust, noise — as what it gives it.

---

## Why sandbox?

AI coding agents that operate directly on the host filesystem carry maximum risk: a single `rm -rf ~` or `git push --force` can destroy your working environment, SSH keys, and git configuration. 

This MCP routes all AI operations through **disposable Docker containers** with structural safety guarantees:

| Guarantee | Mechanism |
|---|---|
| AI operations never touch the host | All file ops, package installs, and test runs happen inside the container. |
| No network by default | `allow_network=True` must be explicitly set. AI can't accidentally call external APIs or push to remotes. |
| Non-root execution | Container runs as unprivileged user `sandbox`. No `sudo`, no system package modifications. |
| VCS tokens stay host-side | The container never receives a `GITHUB_TOKEN`. Token values are masked in all output (`KEY=***`). |
| Audit trail | Every operation is recorded in an append-only journal (`~/.sunaba/journal.log`). |

### Reducing host permissions
By placing a disposable container as a middle layer, you can turn off broad host execution permissions in your AI client. Host-level shell tools become unnecessary for the vast majority of tasks.

---

## Design Philosophy

Four principles drive every decision in Sunaba:

1. **Security and convenience are not a tradeoff — the sandbox dissolves it**: Inside the container, the AI operates with maximum convenience (like a local shell). To the host, the AI is structurally cut off, allowing you to disable local shell permissions.
2. **AI-first: return structure and diffs, not raw logs**: Sunaba strips logs, ANSI colors, and library stack traces, returning only structured test failures (`{test, error, file, line}`) or diffs. This keeps context windows small and reasoning accurate.
3. **Defend the sandbox boundary — not "dangerous" commands**: We do not try to filter "dangerous" commands inside the container. Instead, we strictly isolate the boundary using a default-deny Egress Proxy and host-side token resolution.
4. **The payload never passes through the LLM**: Issue bodies, codebases, and diffs stay inside the container. The AI only carries resource handles, run IDs, and structured summaries.

For the full detailed rationale, see [Design Decisions](docs/design.md).

---

## Typical Workflow

The core developer workflow is centered around a single 5-step loop:

```
sandbox_initialize    # Pull the repository into a fresh container (clone_repo="owner/name")
    ↓
write_file_sandbox    # Edit in place (or transform_file for bulk/computed edits)
    ↓
verify_in_container   # Lint + type-check, then runs tests → structured result
    ↓
checkpoint            # Cheap in-container Git save point (no token, no gate)
    ↓
publish               # Stage, squash checkpoints, push, and create PR (VCS token host-side)
```

The heavy data (repositories, full diffs, raw logs) lives and dies inside the container. The model only carries `run_id`s and structured summaries.

---

## Quick Start (WSL2 / Linux Daemon Setup)

Sunaba is designed to run as a resident background service (daemon) under `systemd` to avoid stdio client timeouts (60 seconds) and support persistent token rotation.

Follow these three phases to install and start the service:

### Phase 1: Install
Create a Python virtual environment and install the package from PyPI:
```bash
python -m venv ~/.local/share/sunaba-venv
~/.local/share/sunaba-venv/bin/pip install sunaba
```

To track unreleased `main` instead:
`~/.local/share/sunaba-venv/bin/pip install git+https://github.com/masuda-masuo/sunaba`

### Phase 2: Setup (Keystore Setup)
The operator scripts live in the repository — the PyPI package ships the server,
not the scripts — so clone it first:
```bash
git clone https://github.com/masuda-masuo/sunaba
cd sunaba
```

Register your GitHub App credentials into the OS keystore:
```bash
./scripts/setup.sh
```

### Phase 3: Enable the systemd Service
```bash
./scripts/install-systemd.sh ~/.local/share/sunaba-venv
```

For client configurations (such as connecting Claude Desktop via `mcp-remote`) and advanced configurations, see the [Daemon Setup Guide](docs/daemon_setup.md).

---

## Prerequisites & First-Run Pitfalls

*   **Docker daemon must be running**: `docker info` should succeed before you start.
*   **The first initialization can take several minutes**: The first call to `sandbox_initialize` pulls the base sandbox images from GHCR. If using stdio, this can trigger client timeouts. We recommend using the [systemd Daemon Setup](docs/daemon_setup.md) to handle this.
*   **Network is off by default**: Anything that touches the network inside the container (e.g. `package_install`, `publish`) requires setting `allow_network=True` during initialization.

### Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| `Cannot connect to the Docker daemon` | Docker isn't running | Start Docker Desktop or `dockerd`. |
| `permission denied` on `docker.sock` | User isn't in `docker` group | `sudo usermod -aG docker $USER` and log back in. |
| `sandbox_initialize` times out | Pulling image from GHCR exceeds 60s client timeout | Switch to the systemd background daemon. |
| `BLOCKED by egress proxy` | Host not allowlisted or `allow_network` is false | Pass `allow_network=True` or add the host to `SUNABA_ALLOWED_EGRESS_HOSTS`. |

---

## Documentation Map

Dive deeper into specific topics:

*   **[Daemon Setup Guide](docs/daemon_setup.md)**: Detailed instructions on running Sunaba as a background `systemd` user service, configuring token rotation, and connecting IDE clients.
*   **[Security & Network Containment](docs/security.md)**: The Egress Proxy design, allowed host configurations, and token isolation details.
*   **[Sandbox Images](docs/sandbox_image.md)**: Details on the `base`, `python`, and `go` Docker images, included tools, and language auto-detection.
*   **[MCP Tool Reference](docs/tools.md)**: A complete reference table of every tool — 22 by default, plus 5 opt-in observability tools.
*   **[Observability & Dashboard](docs/observability.md)**: The local web dashboard, append-only execution logging, replay traces, and push notification triggers.
*   **[Contributing Guide](CONTRIBUTING.md)**: Local developer setup instructions (`pip install -e .[test]`) and test commands.
*   **[Design Decisions](docs/design.md)**: Detailed rationale and design logs.
*   **[Changelog](CHANGELOG.md)**: Release version history and migration guides.

---

## Known Limitations

*   **No local export (No host write-back)**: To prevent host contamination, file transfer is strictly one-way (host → container). The only way to export changes from the sandbox is via `publish` to a GitHub repository. Purely local projects that are not hosted on GitHub cannot be round-tripped.
*   **Job state is in-memory**: Background job results (`sandbox_exec_background`) are lost on server restart. Use `run_container_and_exec` for critical one-shot operations.
*   **Job list grows unbounded**: Completed job results accumulate in memory (not an issue for typical short-lived sessions).

---

## License

MIT