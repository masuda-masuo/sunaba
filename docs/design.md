# Sunaba — Design Policy & Feature Roadmap

> Position: This is not just an "MCP for managing Docker", but an infrastructure where an AI can safely execute test, verify, and publish workflows using minimal context.
> The primary focus is not on increasing features, but on suppressing token consumption, preserving reasoning accuracy, and retaining final human audit control.

## Fundamental Goal

**Achieve local-equivalent convenience while structurally enforcing high security.**

Usually, security and convenience are a trade-off. Broadening permissions increases convenience but compromises safety; tightening permissions enforces safety but hinders productivity. This MCP resolves this trade-off by placing a disposable Docker container as an intermediate sandbox layer.

*   **Convenience for the AI**: Local-equivalent execution speed and editing feedback.
*   **Security for the Host**: Structural isolation, allowing operators to safely disable host-level shell access in their AI clients.

If the AI cannot complete a development cycle within the sandbox, it is a tool design issue. Allowing the AI to fall back to the host shell hides sandbox design failures. This fundamental goal serves as the primary axis for all architectural decisions.

---

## 0. Core Philosophy

*   **AI-First**: Do not show everything. Return structured summaries instead of raw logs. Refine details progressively.
*   **Sandbox Boundary Safety & Post-Hoc Audit**: The boundary line to defend is not "is this command dangerous?", but rather **"does this operation cross the sandbox boundary?"** The container is ephemeral; whatever happens inside disappears upon deletion. No arbitrary command gates are enforced inside the container. Instead, boundary-crossing operations (persistent volumes, host mounts, network, external VCS writes) are structurally blocked via static guards (§2). Operations that cannot be blocked statically are recorded in an append-only journal (§8) for **post-hoc audit**.
*   **Clean Environments First**: Prioritize reproducibility, security, and debuggability over performance optimization via shared state. Sandboxes are disposable by default.
*   **Risk Hierarchy**: Allowing the AI direct execution access on the host carries the highest risk (arbitrary filesystem deletion, force-pushing remote branches, exfiltrating local SSH keys). Placing the AI inside a sandbox neutralizes these vectors. The value of this MCP lies in its **guarantees of what the AI cannot do** (default-off network, non-root execution, host-side token resolution).
*   **Minimizing Host Permissions (Preventing Approval Fatigue)**: Running an AI on the host results in frequent bash/powershell approval prompts. This leads to user approval fatigue, eventually prompting users to allow all actions blindly. Using this MCP concentrates all execution inside the container, allowing host shell permissions to remain strictly disabled.
*   **Scope Delineation**: This MCP is a sandbox to drive the "test, verify, and publish" cycle. **Semantic code search and indexing are excluded from this server** (§1). External VCS writes (push, PR, issue creation) are permitted only under strict boundary-crossing conditions (§2.2). The user's host-side workspace tree is never modified (§5).
*   **Affordance**: Pre-installed does not mean used. LLMs only use capabilities exposed as **explicit verbs in their tool lists**. Consequently, key development workflows are exposed as first-class tools, balanced against the scope constraints of §1 and §5.
*   **Accident-Resistant Filesystem Design**: Telemetry, logs, and temporary workspace transformations are directed outside the git workspace (e.g. to host-local `~/.sunaba/` or container-local `/tmp/`), preventing the AI from accidentally staging or committing intermediate build/patch files. See [Filesystem Layout & Safety Design](design_filesystem_layout.md) for details.

---

## 1. Out of Scope (What We Do Not Do)

To prevent scope creep, we define clear boundaries based on whether an operation acts upon a live session, and whether it requires maintaining/persisting an index, embedding database, or code graph.

*   **Code Understanding & Indexing Layers**: **Excluded from this MCP.**
    *   *Excluded*: Embedding-based semantic search, code-RAG stores, and persistent code graphs. These duplicate existing code-RAG MCPs and complicate maintenance.
    *   *Allowed (Container-local CLIs)*: `ripgrep`, `ast-grep`, and `ctags` executing inside the container. These do not persist state and are cleaned up when the container is destroyed.
*   **Container Snapshots & Restores**: Deferred (contradicts the clean environment principle).
*   **Temporary Port Exposure & Complex Networks**: Deferred. Multi-container orchestration is delegated to docker-compose where necessary.
*   **In-Container Arbitrary Command Gates**: **Excluded.** Command-filtering inside the container is unreliable. We only protect the sandbox boundary (§2).
*   **External VCS Management**: **Excluded.** Issue triage, pull request thread management, and GitHub Projects synchronization are delegated to dedicated GitHub MCP servers (§11).
*   **Review Execution**: **Included** (#475). Checking out PR branches, running test suites, compiling findings, and posting review summaries are treated as part of the code modification lifecycle. "Thread management" remains out of scope. See §15 for details.

---

## 2. Security & Boundaries

Security is not an interactive check; it is a set of static guardrails enforced from the first commit. **Only operations crossing the sandbox boundary are guarded.**

### 2.1 Static Guardrails (Enforced)
*   Non-root user execution (`--user`) is enforced by default.
*   Privileged mode (`--privileged`) is strictly forbidden.
*   Mounting dangerous sockets (such as `/var/run/docker.sock`) is blocked.
*   Host mounts are restricted to an allowlist.
*   Resource limits (CPU, Memory, PIDs) are enforced; networking is disabled by default.
*   **Image digests are pinned** (`image@sha256:...`) instead of using tags to ensure reproducibility.

### 2.2 Boundary-Crossing Tokens
We avoid generic "confirm dangerous command" interactive prompts. Instead, we enforce token requirements on boundary-crossing operations.

**VCS Writes (Token Required)**
*   VCS writes (`git push`, PR creation, issue comments) require token resolution.
*   These are executed in a single-shot fashion (two-step confirmation via `dry_run` is deprecated). Approval is handled by the MCP client, while structural protection is managed by the egress proxy (allowlist + short-lived authorization windows).

**VCS Reads (Network Enabled + Logged)**
*   VCS reads (`gh issue view`, `gh pr view`) are non-destructive and do not require interactive confirmation. However, because they cross the network boundary, they require an explicit network-enable flag and are logged in the execution journal (§8).

**In-Sandbox Commands (Unrestricted but Logged)**
*   Arbitrary commands running inside the container are not gated, but they are fully logged in the journal (§8) for auditing.

### 2.3 Egress Proxy (The Core Guard)
The structural security of the sandbox boundary relies on an Egress Proxy sidecar container that intercepts and gates all outgoing network requests. It enforces:
1.  **Path Containment**: Direct IP/TCP connection blocking (forcing HTTP proxy traversal).
2.  **Destination Allowlist (Default-Deny)**: Restricting external domains.
3.  **Write Allowlist**: Preventing unauthorized Git pushes or VCS writes.

For detailed information on the proxy architecture, CA certificate volume management, error messages, and network isolation configurations, see the dedicated [Security & Network Containment](security.md) documentation.

---

## 3. Token Reduction (LLM Context Optimization)

We optimize token efficiency across the edit-verify loop using three techniques:

### 3.1 Server-Side State & Resource Handles
*   **`run_id` as the execution anchor**: The AI can fetch subsequent logs or re-run failed test suites by referencing a specific `run_id` without resubmitting large context blocks.
*   **Resource Handles for Large Outputs**: Large outputs (like coverage maps or JSON payloads) are returned as resource handles (e.g., `resource://run/123/coverage`) along with their byte sizes.
*   **File-Backed Issue Views**: `issue_view` writes the issue body to a file inside the container and returns only a summary and a file handle to the LLM. The AI can read the full text via `read_file_range` if needed.

### 3.2 Returning Diffs Instead of Full Files
*   **Failure Compression**: Identical errors are grouped and compressed (`compress_failures`).
*   **Result Cache**: The execution result cache was removed (#457) as it led to cache-freshness bugs under default-deny policies.
*   **VCS Push Summary**: Code changes pushed via `publish` are summarized; raw diffs are not sent back to the LLM unless requested.

### 3.3 Log De-noising
*   ANSI color codes, carriage returns, and progress bars are stripped.
*   **Stack Trace Pruning**: Library frames (such as `site-packages` or internal pytest frameworks) are removed, leaving only application-level code frames.
*   **Minimal Success Outputs**: Successful test runs return a clean status line: `{status: "ok", passed: 120, duration: 4.2s}`.

### 3.4 Auxiliary Mechanisms
*   **Token Budget Parameters**: `max_output_tokens` truncates outputs to fit budget constraints, appending a pagination token.
*   **Batch Commands**: Allows executing `[cmd1, cmd2, cmd3]` in a single round-trip.

### 3.5 Tool Descriptions: Map, Contract, Nudge (#550)

Tool descriptions are resident context: they are sent on every request, for
every tool, whether or not the tool is called.  They had grown to ~34KB because
each docstring re-taught the whole workflow ("call `verify_in_container` before
`publish`...") to an agent that had already read the same sentence in the twenty
tools above it.  The fix is not shorter prose but assigning each layer one job,
so that no fact is stated twice:

| Layer | Answers | Sent | Cost |
|---|---|---|---|
| **Map** — server `instructions` | "How do these tools fit together?" | Once per session | Paid once |
| **Contract** — tool docstrings | "How do I call *this* tool, and how does it fail?" | Every request | Paid per turn |
| **Nudge** — `recommended_next_action` | "You are about to do the wrong thing *right now*." | Only on contradiction | Paid on error |

*   **The map is the `instructions` field.** The cross-tool workflow (init →
    explore → edit → verify → publish, and which dedicated tool replaces which
    raw shell command) is stated once, at the server level.  Budget: under 2KB
    UTF-8 — Claude Code truncates tool descriptions at 2KB, and instructions get
    roughly one screenful of attention.
*   **Docstrings state the interface contract, not the workflow.** Arguments,
    side effects, mutually exclusive modes, and failure behavior — the things
    that are true of the tool in isolation and cannot be derived from the map.
    A docstring must not restate the workflow, because the map already carries
    it.  Total description weight drops from ~34KB to ~16KB.
*   **Nudges fire only on contradiction.** The server knows runtime state the
    agent cannot see: whether a container still exists, whether the verify gate
    ever passed for it in this session (`verify_state.py`).  When an action
    contradicts that state, the result carries an advisory
    `recommended_next_action` field — a missing container points at
    `sandbox_initialize`, a `publish` with no recorded verify pass and
    `skip_verify_gate=False` blocks with `status=error` (Issue #615).
    When `skip_verify_gate=True` the nudge still fires as advisory.
    Journal analysis behind #550 showed that unconditional nudges are
    mostly noise, so they fire only on contradiction, and they never
    block: the nudge is a hint, and the gate is the gate.

**Constraint worth knowing before editing a docstring:** FastMCP drops
everything from the `Args:` line onward when it builds the visible tool
description.  Any contract the model must see has to appear *before* the `Args:`
block, or it is written for nobody.

The verify-state map is deliberately process-local and in-memory: the nudge
path is advisory, so a record lost on restart degrades to a missing hint.
The `publish` gate (Issue #615) blocks on a missing record only at call
time within the same server session — a restart resets the map and the
gate re-blocks, which is the correct conservative behaviour.

---

## 4. Structured Test Results

To optimize AI reasoning, test results are parsed into structured JSON format instead of returning raw console logs:

```json
{
  "status": "failed",
  "duration": 12.3,
  "passed": 120,
  "failed": 2,
  "failures": [
    { "test": "test_login", "error": "AssertionError", "file": "auth/login.py", "line": 42 }
  ]
}
```

Support is provided for three testing frameworks:
*   **Pytest**: Parsed via `pytest-json-report` (`--json-report`).
*   **Jest**: Parsed via `jest --json`.
*   **Go Test**: Parsed via `go test -json`.

---

## 5. Edit/Verify Subsystem

The edit/verify subsystem is designed to **quickly edit code and verify test failures** inside an active session without leaving the sandbox.

### Core Tools
*   **`search_in_container`**: Runs `ripgrep` (lexical search) or `ast-grep` (structural search), returning `{file, line, text}` matches.
*   **`read_file_range`**: Reads files using line ranges with `offset` and `limit`.
*   **`write_file_sandbox`**: Declarative file editor supporting overwrite, line ranges, appending, and search-and-replace (`old_str` to `new_str`).
*   **`lint_in_container` / `type_check_in_container`**: Single-file checkers (`ruff`, `pyright`, `eslint`). `lint_in_container` supports `fix=True` to run autofixes on the target file.
*   **`verify_in_container`**: Runs linter and type check gates before executing the test runner. If either gate fails, testing is aborted and warnings are returned. Automatically selects the runner based on the project language (pytest, jest, go test). Returns structured JSON diffs of modified files (`unstaged` and `staged` additions/deletions).

### Editing Modalities
Code modifications are divided into two distinct approaches:
*   **Declarative (`write_file_sandbox`)**: Used when the exact replacement content is known. This is the primary path for targeted edits.
*   **Imperative (`transform_file`)**: Used when content needs to be calculated (regex replacements, AST rewrites, patching via `git apply`). The code is passed as a string and executed securely, returning the resulting diff.
*   **`apply_patch` (Removed)**: Manual patch application was deprecated (#259). LLM-generated diffs suffer from high syntax failure rates, consuming excessive tokens. Automated patching is now handled via the `transform_file` API.

### File Operations (`tools/file.py`)
Provides host-to-container copy operations:
*   `list_files`: Lists files inside the container with pattern filtering.
*   `copy_file`: Copies a single file from the host into the container.
*   `copy_project`: Packages a host directory as a tarball and unpacks it inside the container.
*   *Note: File transfers are host-to-container only. Container-to-host exports are blocked for security.*

---

## 6. Output Control

*   `verbose` parameter support: `error_only`, `summary`, and `full` (defaults to `summary`).
*   Truncation formatting: `{ "shown": 20, "total_lines": 5000, "truncated": true }`.
*   Pagination: `offset` and `limit` arguments are supported, returning `next_offset` and `has_more`.

### 6a. Unified Error Contract (#467)
All tool errors are returned in a standard JSON shape:
```json
{"status": "error", "error": "<human readable message>"}
```
Tool-specific fields (such as `gate_passed` in `verify_in_container`) may be included alongside the standard fields.

### 6b. Search Result Metadata (#469)
`search_in_container` returns pagination metadata:
```json
{"matches": [...], "shown": 20, "total": 150, "truncated": true, "next_offset": 20}
```

---

## 7. Transport Options (Timeout Mitigation)

> Problem: Standard `stdio` transport is capped at a ~60-second timeout by many MCP clients. Operations like pulling base images, compiling test runs, or copying repositories can exceed this limit.

We resolve this by using HTTP-based transport options (such as `streamable-http`) under a background systemd user service, which prevents client timeouts during slow operations.

| Transport | Recommended for | Characteristics |
|---|---|---|
| `streamable-http` (Recommended) | Production / WSL2 | Binds to localhost. Supports bidirectional streaming and prevents client timeouts natively. |
| `sse` | Alternative daemon | Server-Sent Events over HTTP. |
| `http` | Alternative daemon | Stateless HTTP transport. |
| `stdio` | Local Debugging only | Standard I/O. Subject to client-side timeouts during slow setup phases. |

For detailed instructions on deploying the server on headless GCE instances or VM nodes, simulating DBus sessions, and forwarding remote loopback ports, see the dedicated [Headless VM & CI/CD Deployment Guide](headless_setup.md).

---

## 8. Post-Hoc Audit (Safety Net)

Because arbitrary commands running inside the container are ephemeral, we do not intercept them. Instead, we log all actions to an append-only journal. Human validation shifts from **pre-execution approval** to **post-hoc auditing**.

---

## 9. Human Observability & Auditing

The principal safety net shifts from pre-execution approval to post-hoc auditing. Sunaba provides an append-only journal, replay traces, a local web dashboard, and real-time push alerts to facilitate easy human review.

For complete setup guides, log rotation policies, dashboard CLI flags, and notification triggers, see the dedicated [Observability & Dashboard](observability.md) documentation.

### 9.1 Logging Matrix (#359)
Every tool must record its operations in the journal:

| Tool | Journal Method | Operation Name | Description |
|---|---|---|---|
| `sandbox_initialize` | `record_initialize` | `initialize` | Container boot |
| `sandbox_exec` | `record_exec` | `exec` | Shell execution |
| `sandbox_exec_background` | `record_exec` (exit=-1) | `exec` | Async execution |
| `sandbox_exec_check` | `record_tool_use` | `tool_use` | Check status |
| `sandbox_stop` | `record_stop` | `stop` | Stop container |
| `write_file_sandbox` | `record_file_write` | `write_file` | File edit |
| `transform_file` | `record_tool_use` | `tool_use` | Scripted edit |
| `copy_project` / `copy_file` | `record_copy` | `copy_project` | Copy action |
| `publish` | `record_boundary_crossing` | `boundary_crossing` | Push to GitHub |
| `sandbox_issue_write` | `record_boundary_crossing` | `boundary_crossing` | Write issue |
| `sandbox_pr_review_write` | `record_boundary_crossing` | `boundary_crossing` | Post PR review |
| `sandbox_attach` | `record_tool_use` | `tool_use` | Attach to container |

---

## 10. Multi-Service Test Environments
Multi-service orchestration helper commands were removed (#438) as they were unused. Running `docker compose` inside the container is blocked by our static guardrails because `/var/run/docker.sock` is not mounted. Multi-service integration testing remains out of scope.

---

## 11. External VCS Integration

This MCP provides direct access to the **ingress (cloning/issues)** and **egress (publishing)** steps of the cycle.

*   **`issue_view`**: Downloads issue text to a container file and returns a summary and file handle to the LLM.
*   **`sandbox_initialize(clone_repo=...)`**: Clones the repository directly inside the container at startup.
*   **`publish`**: Commits and pushes changes to GitHub. If the egress proxy blocks `git push`, the tool fails immediately and does not fall back to the GitHub Objects API (#401) to avoid masking configuration issues.

### 11.1 VCS Operations Model

| Layer | Tools | Authentication | Gates | Boundary |
|---|---|---|---|---|
| **Save** | `checkpoint` / `checkpoint_list` | None | None | Container-local |
| **Restore** | `checkpoint_restore` | None | None | Container-local |
| **Egress** | `publish` | Host-resolved token | Pre-publish verification | Push to GitHub |

*   **Squashing**: `publish` squashes all local checkpoints into a single git commit before pushing.

### 11.2 Host-Side Token Resolution
To support persistent connections when running under systemd services, tokens are resolved on the host side:

| Provider | Mechanism | Description |
|---|---|---|
| **Static PAT** | `GITHUB_TOKEN` env | Static token injected into the server process. |
| **GitHub App** | `AppTokenProvider` | Periodically requests installation tokens using a host-side private key (#223). |
| **Token Broker** | `token_broker.py` | Requests tokens via a local secure broker binary (#235). |

*   **Resolution Order**: Resolves credentials in the order: **Token Broker → GitHub App → Static PAT**.

For in-depth explanations on key-rotation threads, host-to-proxy credential injection windows, and automatic output token scrubbing, see the dedicated [VCS Authentication & Token Lifecycle](auth_flow.md) document.

---

## 12. Sandbox Docker Images

Sandbox image variants allow language toolchains to be decoupled from a single monolithic parent image. The server dynamically selects the appropriate variant by scanning project markers.

For details on the image layer hierarchy, pinned digests, and language detection rules, see the dedicated [Sandbox Images](sandbox_image.md) and [Multi-Language Support Design](design_multilang_support.md) documents.

---

## 13. Container Lifecycle

*   `sandbox_initialize`: Spawns sandbox containers. Emits progress notices during image pulling and package installs to prevent timeouts (#298).
*   `sandbox_stop`: Tears down containers. Warns if there are unpushed checkpoints.
*   `sandbox_list_containers` / `sandbox_attach`: Discover and reconnect to active containers (#478).
*   `_reap_idle_containers`: Reclaims inactive containers based on idle timers.

### 13.1 Garbage Collection Policy (#480)
*   **Idle Tracking**: Containers expose `idle_seconds` based on the timestamp of their last logged journal activity.
*   **Automatic GC**: Setting `SUNABA_CONTAINER_TTL_SECONDS` to a positive integer automatically stops containers that have been idle longer than the TTL. Disabled by default.
*   **Cleanup Conventions**: It is recommended to call `sandbox_stop` when closing a pull request or issue.
*   **Orphan Reclamation**: Spun-off setups that crash or time out before completing initialization are cleaned up by the orphan reaper.

---

## 14. Review Flow (Review Execution)

PR reviews are treated as part of the sandbox modification lifecycle (#475).

### 14.1 Review Execution Workflow
1.  **Fresh Sandbox Checkout**: Verify the pull request branch in a clean container (`pr=N`) to ensure the PR is self-contained.
2.  **Inspect Diffs**: Analyze changes using structured diffs (#476).
3.  **Read Context**: Retrieve historical contexts and issues via Shiori.
4.  **Submit Review**: Post findings using the host-side `sandbox_pr_review_write` tool (#477).

### 14.2 Session Separation & Model Hierarchy
We organize agent workflows into specialized roles. State is managed externally via Git repositories and issue logs:

| Role | Model Tier | Input | Output | Container Lifecycle |
|---|---|---|---|---|
| **Architecture** | High (Reasoning) | Requirements | Decoupled Issues | None |
| **Implementation** | Medium (Coding) | Target Issue | PR / Commits | Attach to `issue-N` |
| **Validation** | Low (Running) | PR Branch | Test status JSON | Fresh `pr=N` container |
| **Audit** | High (Review) | PR + Test results | Review comments | None |

*   **Failure Escalation**: If a low-tier coding agent fails `verify_in_container` twice, execution is halted, and the issue is escalated to a high-tier reasoning agent.

---

## 15. Architectural Decision Log

### #475: PR Review Execution Scope
*   **Decision**: Split PR reviews into "management" (out of scope) and "execution" (in scope).
*   **Context**: Checking out PRs, executing tests, and producing diffs benefit from sandbox isolation. Review comments are posted using host-side API wrappers.

### #481: Multi-Agent Model Hierarchy
*   **Decision**: Documented guidelines for distributing work across different model tiers (reasoning vs. coding). State is kept inside the repository and shared containers.

### #473: v1.0.0 Compatibility Policy
*   **Decision**: Bumped version to `1.0.0` and defined stability policies (tool names, environments, and argument schemas are frozen under SemVer).

### #531 / #534: Project Rename to Sunaba
*   **Decision**: Retracted the unreleased `1.0.0` tag, renamed the package from `code-sandbox-mcp` to `sunaba`, and reset the version to `0.8.0` to finalize name transitions before freezing the public API.

### #495 -> #506: Egress Proxy Default-Deny
*   **Decision**: Enforce a default-deny policy on the egress proxy. Hostnames not explicitly defined in `SUNABA_ALLOWED_EGRESS_HOSTS` are blocked (Layer ②).

### #509: Default-On Egress Proxy
*   **Decision**: Automatically enable the egress proxy for all container sessions using network access. If the proxy fails to start, the sandbox fails to initialize (fail-closed).
