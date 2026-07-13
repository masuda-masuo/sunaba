# MCP Tool Reference

This document lists all available MCP tools exposed by the Sunaba control plane, categorized by functional modules.

Required parameters are listed first; `(opt)` marks optional ones. The parameter
columns are checked against the real tool signatures by
`tests/test_tools_doc.py`, so a signature change that is not reflected here
fails CI (#573).

---

## 1. Lifecycle Tools
Manage the lifecycle of disposable Docker sandbox containers.

| Tool Name | Parameters | Description |
|---|---|---|
| `sandbox_initialize` | `clone_repo` (opt), `repo` (opt), `pr` (opt), `image` (opt), `allow_network` (opt), `pip_extras` (opt), `name` (opt) | Spawns a sandbox container. Returns a 12-character `container_id`. `clone_repo="owner/name"` clones in the same call; `repo` + `pr=N` checks out a PR branch instead. |
| `sandbox_stop` | `container_id`, `force` (opt) | Stops and removes the sandbox container. |
| `run_container_and_exec` | `commands` (opt), `image` (opt), `allow_network` (opt), `clone_repo` (opt) | One-shot execution: creates a container, runs the commands sequentially, and tears it down. |
| `sandbox_list_containers`| — | Lists all currently active and managed containers along with `idle_seconds`. |
| `sandbox_attach` | `name_or_id`, `session_label` (opt) | Connects a client session to an existing running container by name (from `sandbox_initialize(name=...)`) or ID prefix. |

---

## 2. Execution Tools
Run commands and manage packages inside the container.

| Tool Name | Parameters | Description |
|---|---|---|
| `sandbox_exec` | `container_id`, `commands` or `argv`, `working_dir` (opt), `timeout` (opt), `verbose` (opt) | Runs commands synchronously inside the container. `commands` are chained with `&&` through a shell; `argv` runs an argument vector directly (no shell quoting). Outputs structured results with pagination options. |
| `sandbox_exec_background` | `container_id`, `commands`, `working_dir` (opt) | Spawns commands in the background. Returns a `job_id` immediately. |
| `sandbox_exec_check` | `container_id`, `job_id` | Checks the status of a background job. Returns output if finished, or `"running"`. |
| `package_install` | `container_id`, `packages` (opt), `editable` (opt), `requirements` (opt), `extras` (opt), `constraints` (opt), `upgrade` (opt) | Structured wrapper for package installs (`pip`/`uv`). Returns installed package versions and avoids log pollution. |

---

## 3. File Operations
Read, write, and copy files inside the sandbox.

| Tool Name | Parameters | Description |
|---|---|---|
| `write_file_sandbox` | `container_id`, `file_name`, `file_contents`, `dest_dir` (opt), `old_str` (opt), `start_line` / `end_line` (opt), `append` (opt) | **Primary edit path.** With no edit mode given the file is fully overwritten; `old_str` replaces an exact string, `start_line`/`end_line` replace a line range, `append=True` appends. The edit modes are mutually exclusive. |
| `edit_symbol` | `container_id`, `file_path`, `symbol`, `new_code`, `line` (opt) | **Symbol edit path.** Locates a function/class/method by name via AST (decorators included) and replaces the whole definition with `new_code`; `new_code=""` deletes it. Nothing is written unless the edited file parses. Returns the resolved location and a unified diff. Python files only. |
| `transform_file` | `container_id`, `file_path`, `code` | **Imperative edit path.** Computes new file content via a Python `transform(text) -> str` script inside the container and returns a unified diff. |
| `read_file_range` | `container_id`, `file_path`, `offset` / `limit` (opt), `start_line` / `end_line` (opt) | Reads a slice of a file (pagination by line numbers) to prevent context flooding. |
| `list_files` | `container_id`, `path` (opt), `max_depth` (opt), `pattern` (opt) | Recursively lists file paths inside the container starting at the specified path. |
| `copy_project` | `container_id`, `local_src_dir`, `dest_dir` (opt) | Copies a host directory into the container using streamed tar archives. |
| `copy_file` | `container_id`, `local_src_file`, `dest_path` (opt) | Copies a single file from the host into the sandbox. |

---

## 4. Edit & Verify Subsystem
Used by AI models to search, lint, type check, and run tests.

| Tool Name | Parameters | Description |
|---|---|---|
| `search_in_container` | `container_id`, `pattern`, `path` (opt), `mode` (opt: lexical/structural), `glob` (opt), `output_mode` (opt) | Searches code using `ripgrep` (lexical) or AST-based `ast-grep` (structural). Returns structured occurrences. |
| `lint_in_container` | `container_id`, `file_path`, `fix` (opt) | Lints a file using `ruff` (Python) or `eslint` (JS/TS). Passing `fix=True` triggers autofixes. |
| `type_check_in_container` | `container_id`, `file_path` | Runs static type checkers (`pyright` for Python, `tsc` for TypeScript). |
| `verify_in_container` | `container_id`, `path`, `test_filter` (opt), `pytest_args` (opt), `language` (opt), `working_dir` (opt) | **Pre-publish gate.** Runs the lint and type gates as a precondition, then the project's tests (`pytest`/`jest`/`go test`). Returns structured test results plus a diff summary. |
| `diff_in_container` | `container_id`, `base` (opt), `path` (opt), `raw` (opt) | Returns a structured JSON summary of changes against `base` (per-file counts), or hunk objects when `path` names a single file. |

---

## 5. VCS & Version Control (GitHub Integration)
Integrate with GitHub issues, check out pull requests, and commit/publish changes.

| Tool Name | Parameters | Description |
|---|---|---|
| `clone_repo` | `container_id`, `repo`, `branch` (opt), `dest_dir` (opt) | Clones a repository inside the container. Requires `allow_network=True` at init. To check out a PR branch, use `sandbox_initialize(repo=..., pr=N)` — `clone_repo` has no `pr` parameter. |
| `issue_view` | `container_id`, `repo`, `issue_number`, `save_to` (opt) | Downloads a GitHub issue thread and saves it to a file in the container, returning a summary. |
| `checkpoint` | `container_id`, `message`, `working_dir` (opt) | Commits changes locally in the sandbox. Creates a cheap save point before editing. |
| `checkpoint_list` | `container_id`, `working_dir` (opt), `limit` (opt) | Lists all unpushed local checkpoints. |
| `checkpoint_restore` | `container_id`, `sha`, `working_dir` (opt) | Discards changes and rolls back the working tree to a previous checkpoint. |
| `publish` | `container_id`, `repo`, `branch`, `message`, `create_pr` (opt), `pr_title` (opt), `pr_body` (opt), `base_branch` (opt), `allow_force_push` (opt) | Stages all changes, squashes unpushed checkpoints, pushes to GitHub, and — **only when `create_pr=True`** — opens a pull request. `create_pr` defaults to `False`, so passing `pr_title` alone pushes without creating a PR. `pr_title` is required when `create_pr=True`. |
| `sandbox_issue_write` | `container_id`, `repo`, `method` (create/comment), `title` (opt), `body` (opt), `issue_number` (opt) | Creates a GitHub issue (`method="create"`, needs `title`) or comments on an existing issue or PR (`method="comment"`, needs `issue_number`). Called host-side; no token reaches the container. |
| `sandbox_pr_review_write` | `container_id`, `repo`, `pr`, `event`, `body` (opt), `comments` (opt) | Submits a PR review (approves/requests changes/comments) with optional inline line comments from the host. |

---

## 6. Observability Tools (Opt-in)
Available only when the environment variable `SUNABA_OBSERVABILITY_TOOLS=1` is set.

| Tool Name | Parameters | Description |
|---|---|---|
| `sandbox_read_journal` | `run_id` (opt), `max_entries` (opt), `session_label` (opt) | Reads the append-only lifecycle execution logs from `~/.sunaba/journal.log`. |
| `sandbox_trace` | `run_id`, `output_format` (opt) | Generates a JSON or HTML replay trace showing exactly what the server executed for a specific run. |
| `sandbox_list_runs` | — | Lists all run IDs recorded in the on-disk journal. |
| `sandbox_journal_path` | — | Returns the absolute path to `journal.log`. |
| `sandbox_trace_dir` | — | Returns the absolute path to the directory hosting trace files. |
