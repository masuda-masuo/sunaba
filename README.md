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
| No network by default | Bare initialization has no container network; `clone_repo`, `pr`, or `branch` auto-enable it. A bare container can set `allow_network=True` for package installation or other allowed network work. Container traffic remains subject to the egress allowlist. |
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
write_file/edit_file  # Create or edit files (transform_file for bulk/computed edits)
    ↓
verify_in_container   # Lint + type-check, then runs tests → structured result
    ↓
checkpoint            # Cheap in-container Git save point (no token, no gate)
    ↓
publish(create_pr=True)  # Stage, squash checkpoints, push, and create PR (VCS token host-side); default is push only, and pr_title alone does not create a PR
```

The heavy data (repositories, full diffs, raw logs) lives and dies inside the container. The model only carries `run_id`s and structured summaries.

---

## Quick Start (WSL2 / Linux Daemon Setup)

Sunaba is designed to run as a resident background service (daemon) under `systemd` to support persistent token rotation and long-running Docker operations without client timeouts.

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
*   **The first initialization can take several minutes**: The first call to `sandbox_initialize` pulls the base sandbox images from GHCR. We recommend running Sunaba as a [systemd daemon](docs/daemon_setup.md) to handle long-running operations.
*   **Container network is off by default**: Bare initialization has no network. `clone_repo`, `pr`, and `branch` enable it automatically; for a bare container that needs package installation or other allowed outbound access, set `allow_network=True`. The egress allowlist still applies. `publish` uses a host-side VCS token for GitHub operations.

### Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| `Cannot connect to the Docker daemon` | Docker isn't running | Start Docker Desktop or `dockerd`. |
| `permission denied` on `docker.sock` | `DOCKER_HOST` unset/wrong (rootless); user not in `docker` group (rootful) | Set `DOCKER_HOST=unix:///run/user/$(id -u)/docker.sock` first; fallback `sudo usermod -aG docker $USER` (⚠️ root-equivalent — see [Daemon Setup](docs/daemon_setup.md)). |
| `sandbox_initialize` times out | Pulling image from GHCR exceeds 60s client timeout | Switch to the systemd background daemon. |
| `BLOCKED by egress proxy` | Container network is disabled or the destination host is not allowlisted | Enable container network access as needed and add the host to `SUNABA_ALLOWED_EGRESS_HOSTS`; `allow_network=True` does not bypass the allowlist. |

---

## Documentation Map

Dive deeper into specific topics:

*   **[Daemon Setup Guide](docs/daemon_setup.md)**: Detailed instructions on running Sunaba as a background `systemd` user service, configuring token rotation, and connecting IDE clients.
*   **[Security & Network Containment](docs/security.md)**: The Egress Proxy design, allowed host configurations, and token isolation details.
*   **[Sandbox Images](docs/sandbox_image.md)**: Details on the `neutral`, `python`, `go`, `js`, `rust`, and `full` Docker images, included tools, and language auto-detection.
*   **[MCP Tool Reference](docs/tools.md)**: A complete reference table of every tool — 36 by default, plus 5 opt-in observability tools.
*   **[Observability & Dashboard](docs/observability.md)**: The local web dashboard, append-only execution logging, replay traces, and push notification triggers.
*   **[Contributing Guide](CONTRIBUTING.md)**: Local developer setup instructions (`pip install -e .[test]`) and test commands.
*   **[Design Decisions](docs/design.md)**: Detailed rationale and design logs.
*   **[Changelog](CHANGELOG.md)**: Release version history and migration guides.

---

## Compatibility Policy

The external contract of the Sunaba server is versioned using [Semantic Versioning](https://semver.org/).

*   **Covered by Semver**: MCP tool names, argument names/types, return shapes, and environment variables. Breaking modifications (e.g. removing a tool or argument) require a minor version bump while in `0.x` (or major bump in `1.x+`).
*   **NOT Covered by Semver**: The sandbox image contents, internal module layout, and digests.

### Behavior changes while on 0.x
- In `0.x`, a change matching either item below is released as a minor version, and CHANGELOG must state it with a "Behavior change:" note:
  - A call that used to succeed is now refused or fails (including fail-closed changes).
  - The meaning or value range of an existing return field changes.
- Adding a new error `step` value or a new return field is not a breaking change, as long as the meaning of existing values is unchanged. If the addition makes a previously successful call fail, the first item above applies.

---

## Known Limitations

*   **No local export (No host write-back)**: To prevent host contamination, file transfer is strictly one-way (host → container). The only way to export changes from the sandbox is via `publish` to a GitHub repository. Purely local projects that are not hosted on GitHub cannot be round-tripped.
*   **Job state lives in the container**: Background job results (`sandbox_exec_background`) are stored as files in the container's `/tmp`, keyed by job ID. They survive server restarts but are lost when the container is destroyed (`sandbox_stop` / reaper) — poll with `sandbox_exec_check` and collect results before stopping the container.
*   **Job files accumulate in /tmp**: Job result files (`.start`/`.out`/`.err`/`.exit`) are removed once `sandbox_exec_check` observes completion, but jobs that finish and are never checked leave their files behind for the container's lifetime. Nothing accumulates in server memory; the cost is `/tmp` usage proportional to container lifetime.

---

## Compact results and saved execution output

The default MCP response format remains unchanged. Clients that consume
`structuredContent` can opt in at the connection URL:
`http://127.0.0.1:8750/mcp?response=compact`.
Combine with a profile using `?profile=implement&response=compact`; keep the same
query parameters on every request, including `tools/list` and `tools/call`.
Use `response=legacy` (or omit it) for clients that parse text-only JSON results.

In compact mode, JSON object results appear directly in `structuredContent`,
arrays under `items`, and scalar/plain-text results under `result`. There is
no second JSON decode. The text block contains only a short summary; the full
payload is not repeated there. The advertised output schema changes only for
this connection. Tool arguments and error flags are unchanged.

When `sandbox_exec` omits output, it returns an `output_id` if the sanitized
full output fits the snapshot limit. Read it with
`read_output(container_id, output_id, offset=100, limit=50)`, or
`read_output(container_id, output_id, tail_lines=20)`; never rerun a command
merely to read its next page. Offsets are zero-based in the full sanitized
stream, before repeated-line/failure compression (stdout on success, combined
stdout/stderr on failure). Each page includes `shown`, `total_lines`,
`next_offset`, `has_more`, and `truncated`.

Foreground snapshots are process-local and bounded: 8 MiB per output, 64 MiB
total UTF-8 payload, at most 128 snapshots, and a one-hour lifetime. Oldest
snapshots are evicted first. They survive container removal but not server
restart, and reads require the same container ID spelling as the exec.
Oversized snapshots return `output_unavailable`; missing/expired IDs return an
explicit error. Background-job storage and `sandbox_exec_check` are unchanged.

## License

MIT
