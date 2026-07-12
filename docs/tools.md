# MCP Tool Reference

This document lists all available MCP tools exposed by the Sunaba control plane, categorized by functional modules.

---

## 1. Lifecycle Tools
Manage the lifecycle of disposable Docker sandbox containers.

| Tool Name | Parameters | Description |
|---|---|---|
| `sandbox_initialize` | `image` (opt), `allow_network` (opt) | Spawns a sandbox container. Returns a 12-character `container_id`. |
| `sandbox_stop` | `container_id` | Stops and removes the sandbox container. |
| `run_container_and_exec` | `commands`, `image` (opt), `allow_network` (opt) | One-shot execution: creates a container, runs the commands sequentially, and tears it down. |
| `sandbox_list_containers`| — | Lists all currently active and managed containers along with `idle_seconds`. |
| `sandbox_attach` | `container_id` | Connects a client session to an existing running container by ID or name prefix. |

---

## 2. Execution Tools
Run commands and manage packages inside the container.

| Tool Name | Parameters | Description |
|---|---|---|
| `sandbox_exec` | `container_id`, `commands` | Runs commands synchronously inside the container. Outputs structured results with pagination options. |
| `sandbox_exec_background` | `container_id`, `commands` | Spawns commands in the background. Returns a `job_id` immediately. |
| `sandbox_exec_check` | `container_id`, `job_id` | Checks the status of a background job. Returns output if finished, or `"running"`. |
| `package_install` | `container_id`, `packages` (opt), `requirements` (opt), `editable` (opt) | Structured wrapper for package installs (`pip`/`uv`). Returns installed package versions and avoids log pollution. |

---

## 3. File Operations
Read, write, and copy files inside the sandbox.

| Tool Name | Parameters | Description |
|---|---|---|
| `write_file_sandbox` | `container_id`, `path`, `content`, `mode` | **Primary edit path.** Supports full overwrite, append, line-range replacement, or flexible `old_str` replacement. |
| `transform_file` | `container_id`, `path`, `code` | **Imperative edit path.** Computes new file content via a Python `transform(text) -> str` script inside the container and returns a unified diff. |
| `read_file_range` | `container_id`, `path`, `offset`, `limit` | Reads a slice of a file (pagination by line numbers) to prevent context flooding. |
| `list_files` | `container_id`, `path` | Recursively lists file paths inside the container starting at the specified path. |
| `copy_project` | `container_id`, `local_src_dir`, `dest_dir` | Copies a host directory into the container using streamed tar archives. |
| `copy_file` | `container_id`, `local_src_file`, `dest_path` | Copies a single file from the host into the sandbox. |

---

## 4. Edit & Verify Subsystem
Used by AI models to search, lint, type check, and run tests.

| Tool Name | Parameters | Description |
|---|---|---|
| `search_in_container` | `container_id`, `query`, `mode` (`lexical`/`structural`) | Searches code using `ripgrep` or AST-based `ast-grep`. Returns structured occurrences. |
| `lint_in_container` | `container_id`, `path`, `fix` (opt) | Lints a file using `ruff` (Python) or `eslint` (JS/TS). Passing `fix=True` triggers autofixes. |
| `type_check_in_container` | `container_id`, `path` | Runs static type checkers (`pyright` for Python, `tsc` for TypeScript). |
| `verify_in_container` | `container_id`, `test_filter` (opt) | **Pre-publish gate.** Runs linters, type checks, and project unit tests (`pytest`/`jest`/`go test`). Returns structured test results. |
| `diff_in_container` | `container_id`, `path` (opt) | Returns a structured JSON summary of unstaged and staged changes (added/deleted lines per file) using git. |

---

## 5. VCS & Version Control (GitHub Integration)
Integrate with GitHub issues, check out pull requests, and commit/publish changes.

| Tool Name | Parameters | Description |
|---|---|---|
| `clone_repo` | `container_id`, `repo`, `branch` (opt), `pr` (opt) | Clones a repository or checks out a specific PR branch inside the container. |
| `issue_view` | `container_id`, `issue_num` | Downloads a GitHub issue thread and saves it to a file in the container, returning a summary. |
| `checkpoint` | `container_id`, `message` | Commits changes locally in the sandbox. Creates a cheap save point before editing. |
| `checkpoint_list` | `container_id` | Lists all unpushed local checkpoints. |
| `checkpoint_restore` | `container_id`, `checkpoint_hash` | Discards changes and rolls back the working tree to a previous checkpoint. |
| `publish` | `container_id`, `branch`, `pr_title` (opt), `force` (opt) | Stages all changes, squashes unpushed checkpoints, pushes to GitHub, and optionally creates a PR. |
| `sandbox_issue_write` | `repo`, `issue_num` (opt), `body` | Creates or comments on a GitHub issue directly from the host. |
| `sandbox_pr_review_write` | `repo`, `pr_num`, `event`, `comments` | Submits a PR review (approves/requests changes/comments) with optional inline line comments from the host. |

---

## 6. Observability Tools (Opt-in)
Available only when the environment variable `SUNABA_OBSERVABILITY_TOOLS=1` is set.

| Tool Name | Parameters | Description |
|---|---|---|
| `sandbox_read_journal` | `max_entries` (opt) | Reads the append-only lifecycle execution logs from `~/.sunaba/journal.log`. |
| `sandbox_trace` | `run_id` | Generates a JSON or HTML replay trace showing exactly what the server executed for a specific run. |
| `sandbox_list_runs` | — | Lists all run IDs recorded in the on-disk journal. |
| `sandbox_journal_path` | — | Returns the absolute path to `journal.log`. |
| `sandbox_trace_dir` | — | Returns the absolute path to the directory hosting trace files. |
