# Sunaba — Use Cases & Documentation Gap Analysis

> Purpose: Inventory of **intended use cases** when driving this MCP via an LLM, and evaluation of **feature coverage** and **onboarding documentation completeness**.
> Criteria: The core design principles in `docs/design.md` (minimized context, boundary containment, post-hoc audit) evaluated against the implementation in `src/sunaba/` and `README.md` (as of July 2026).
> Note: Historical gaps identified here (P1/P2) were addressed in #493–#496. This document has been updated to reflect the resolved items.

---

## 1. Intended Use Cases (UC)

*Legend: ◎ = Fully supported as a primary flow / ○ = Supported with caveats / △ = Partially supported (gaps exist) / × = Out of scope (by design)*

| # | Use Case | Typical Flow | Support |
|---|---|---|---|
| **UC-1** | **GitHub issue-driven bug fixing** | `sandbox_initialize(clone_repo=...)` → `issue_view` → `search_in_container` → `read_file_range` → `edit_file` → `verify_in_container` → `checkpoint` → `publish` | **◎** |
| **UC-2** | **Feature additions & TDD on GitHub** | Same loop as UC-1. If writing tests first: `write_file` (test file) → `verify_in_container(test_filter=...)` → implement code → verify all → `publish` | **◎** |
| **UC-3** | **Checking out and fixing existing PRs** | `sandbox_initialize(repo=..., pr=N)` → edit-verify loop → `publish` | **○** (See §3.4) |
| **UC-4** | **Editing purely local projects** | `copy_project` → edit loop → `verify_in_container` | **△** (See §3.2) |
| **UC-5** | **Disposable code execution & scratch pads** | `run_container_and_exec` or `sandbox_initialize` → `sandbox_exec` | **◎** |
| **UC-6** | **Dependency installation & upgrade verification**| `package_install` (Python) → `verify_in_container` | **○** (Pip only. See §3.3) |
| **UC-7** | **JS / TS project development** | Edit-verify loop (`search` / `eslint` / `tsc` / `jest`) → `verify_in_container` | **◎** (Resolved in #493) |
| **UC-8** | **Go project development** | Edit-verify loop in `sandbox:go` image → `verify_in_container` (via `go test -json`) | **◎** (Resolved in #493) |
| **UC-9** | **Long-running jobs** (compilations, large test suites) | `sandbox_exec_background` → `sandbox_exec_check` (over SSE/HTTP) | **○** (Job dictionary is in-memory) |
| **UC-10**| **Web servers / multi-service integration testing** | Start server inside container → run `curl` from within same container | **△** (See §3.5) |
| **UC-11**| **Human post-hoc audit & code review** | Review via `journal.log` / `traces` / web dashboard / notifications | **◎** |
| **UC-12**| **Writing investigation summaries & comments** | `sandbox_issue_write` | **○** |
| **UC-13**| **Non-GitHub VCS platforms** (GitLab, Bitbucket) | — | **×** (Assumes `gh` and GitHub APIs. Out of scope) |
| **UC-14**| **VCS issue triage & project board management**| Handled by dedicated GitHub MCP servers | **×** (Explicitly out of scope in `docs/design.md` §1) |
| **UC-15**| **Pull Request code reviews** | `sandbox_initialize(pr=N)` → edit loop → `diff_in_container` → `sandbox_pr_review_write` | **◎** (New tools created) |

**Overall Assessment**: The primary target flow of Sunaba ("issue → fix → verify → publish") (UC-1/2) is fully covered with dedicated, first-class tools. The payload containment, structured outputs, and Git checkpoints are highly mature. Functional gaps reside primarily outside this main flow, such as purely local projects (UC-4), dependency installers for JS/Go (UC-6), or running multi-service setups (UC-10).

---

## 2. Use Case × Tool Coverage Matrix

Verification of whether first-class tools exist for each phase of the loop:

| Phase | Tool | Python | JS/TS | Go |
|---|---|---|---|---|
| **Boot** | `sandbox_initialize` (with auto image selection) | ✅ | ✅ | ✅ |
| **Ingress** | `issue_view` / `sandbox_initialize(clone_repo=..., pr=N)` | ✅ | ✅ | ✅ |
| **Search** | `search_in_container` (`ripgrep` / `ast-grep`) | ✅ | ✅ | ✅ |
| **Read** | `read_file_range` / `list_files` | ✅ | ✅ | ✅ |
| **Edit (Decl)** | `write_file` / `edit_file` | ✅ | ✅ | ✅ |
| **Edit (Imp)** | `transform_file` | ✅ | ✅ | ✅ |
| **Lint** | `lint_in_container` (`ruff` / `eslint` with `fix=True`) | ✅ | ✅ | — (Unimplemented) |
| **Type Check**| `type_check_in_container` (`pyright` / `tsc`) | ✅ | ✅ | — (`go vet` not wired) |
| **Test** | `verify_in_container` (structured JSON results) | ✅ pytest | ✅ jest | ✅ go test |
| **Packages** | `package_install` | ✅ pip/uv | — (via `sandbox_exec`) | — (via `sandbox_exec`) |
| **Save/Reset**| `checkpoint` / `checkpoint_list` / `checkpoint_restore` | ✅ | ✅ | ✅ |
| **Egress** | `publish` / `sandbox_issue_write` | ✅ | ✅ | ✅ |
| **Audit** | `journal` / `trace` / local dashboard | ✅ | ✅ | ✅ |

---

## 3. Identified Gaps

### 3.1 [Resolved] Jest / Go Test Structured Verifications
Previously, only pytest results were parsed structurally. JS/Go project test executions were not correctly integrated. This was resolved in #493, and `verify_in_container` now parses and returns structured test results for all three languages.

### 3.2 No Host Write-Back (Local Projects)
By design, file transfer is strictly one-way (host → container) to prevent containerized code from writing back and contaminating the host filesystem. The only supported export mechanism is `publish` (pushing to GitHub). As a result, users cannot round-trip changes to purely local files not tracked on GitHub. 
*   **Resolution**: This constraint is now explicitly documented in the "Known Limitations" section of the `README.md`.

### 3.3 package_install is limited to Python (Pip/Uv)
`package_install` is Python-only. Package managers like `npm`, `yarn`, `cargo`, or `go get` must be run manually via `sandbox_exec`. For JS/Go projects, verbose dependency installation output can pollute the LLM's context window. Adding structured packaging tools for JS/Go is a future refinement.

### 3.4 Reading PR Review Comments
While we can check out PR branches using `pr=N`, there is no tool to fetch PR review comments (unlike issues, which can be viewed via `issue_view`). To help the AI respond to reviewer feedback and run the "checkout → fix reviews → push" loop, we need a `pr_view` tool that downloads comments into a container file and returns a summary to the LLM.

### 3.5 Service Verification Constraints
*   Sunaba intentionally defers exposing container ports to the host (to maintain strict host boundary safety). You can start web servers inside the container and verify them locally using `curl` from within the container, but human developers cannot inspect the UI via a browser.
*   **Docker-in-Docker Restriction**: Because the `/var/run/docker.sock` socket is not mounted inside the container, running `docker compose` from within `sandbox_exec` is structurally impossible.

---

## 4. Documentation Evaluation for Humans

### 4.1 What Is Covered
*   **Core Concepts**: Clear explanations of the sandbox safety boundaries, proxy gates, and post-hoc audits.
*   **Onboarding**: Clean 5-step workflow diagram and quick-start installation blocks.
*   **Troubleshooting**: High-priority first-run pitfalls and solutions.

### 4.2 Opportunities for Improvement
1.  **Onboarding Steps**: Guide users progressively from a basic tokenless setup (reading public repos) to static PATs, and finally to systemd resident setup.
2.  **IDE Client Examples**: Provide configuration examples for IDEs other than Claude Desktop (e.g. Claude Code, Cursor, opencode).
3.  **Host-Permissions Management**: Add instructions showing users how to disable host-level shell access in their AI clients to take full advantage of Sunaba's security isolation.

---

## 5. Documentation Evaluation for LLMs

The documentation exposed to the AI client models via MCP tool descriptions and schema contracts is highly mature:
*   Tool docstrings clearly document execution boundaries, parameters, and intent.
*   Error responses conform to a unified `{status: "error", error: "..."}` shape, making it easy for the AI model to parse and recover from exceptions.

---

## 6. Recommended Actions (Remaining Tasks)

*Note: Historical P1/P2 recommendations from this document have been resolved in #493–#496. The following are the remaining unresolved actions.*

| Priority | Action Item | Category |
|---|---|---|
| **P3** | Investigate implementing `pr_view` (a tool to fetch and display PR review comments) (§3.4). | Feature |
| **P3** | Document client-specific configurations, host permission reduction procedures, and environment variables (§4.2). | Documentation |
| — | Wire Go linters (e.g., `golangci-lint`) and `go vet` into `lint_in_container` / `type_check_in_container` (§3.6). | Feature |
| — | Document the limitation on local-only project round-trips in the `README.md` "Known Limitations" section (§3.2). | Documentation |
| — | Add a step-by-step setup progression guide (from tokenless to full resident setup) (§4.2). | Documentation |
