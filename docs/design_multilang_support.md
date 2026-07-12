# Multi-Language Support Design (Verify Dispatch & Image Splitting)

> **Note**: Certain assumptions in this design document were modified in issue #257. The static scanning (semgrep) layer was removed, the `submit` tool was renamed to `publish`, and internal verification gates were detached from it. 
> Currently, `verify_in_container` is dedicated to running tests, while `lint_in_container` and `type_check_in_container` are independent tools.
>
> Position: Supporting design reference updating §4 (Structured Testing), §5 (Edit/Verify), and §12 (Base Image) of [docs/design.md](design.md) to bring the implementation in line with the multi-language design goals (pytest / jest / go test). The objective is to **decouple multi-language toolchains from a single monolith image and eradicate silent validation failures**.

---

## 1. Background & Gaps

The original design document (§4) specified that version 1.0 must support **pytest, jest, and go test**. However, the initial implementation was heavily coupled with Python, creating several gaps:

*   **Multi-Language Parsers vs. Python-Only Runner**: `test_report.py` implemented `PytestAdapter`, `JestAdapter`, and `GoTestAdapter`, but `run_verify` in `edit_verify.py` unconditionally executed `ruff`, `pyright`, and `pytest` without performing language detection.
*   **Base Image Lacked Node & Go**: The monolithic `docker/Dockerfile.sandbox` was based on `python:3.12-slim` and did not contain Node.js or Go runtimes. There was no environment to run `jest` or `go test`.
*   **Pyright Execution Failure**: `pyright` was installed via the Python wrapper `uv tool install pyright`, which depends on Node.js. In a network-disabled container (which is the default posture), pyright failed to dynamically fetch the Node runtime, leading to unstable type check execution.
*   **Silent Failures (Invisible Errors)**: 
    *   Command runners discarded stderr and forced exit codes to `0` using `... 2>/dev/null || true`. Consequently, "no findings" and "tool execution crashed" returned the same output, resulting in false positives (green builds).
    *   `_run_pytest_verify` returned `status: skipped` if stdout was empty or a parse error occurred. Because skipped tests were not caught by the verification gate, PRs could proceed even if test execution completely failed to run.
    *   Exit code `127` (command not found) was quietly mapped to severity `info` (e.g. `no-linter`), allowing JS/Go projects to bypass all gates since the tools simply didn't run.

---

## 2. Strategy

1.  **Node/JS in the Base Layer; Compilers in Backend Layers**: JS is a cross-cutting layer (needed for Pyright as well as frontend code) and will be bundled into the base image. Specialized compilers/runtimes (like Python or Go) will reside in distinct backend layers.
2.  **Unified Dispatch Matrix / Modular Images**: Language detection rules will support Python, JS, TS, and Go. Missing tools will be treated as first-class `not_available` statuses rather than silent skips.
3.  **Monolith Prevention**: Runtimes required by cross-cutting infra belong in the `base` image. Language-specific development/testing packages belong in their respective backend layers.
4.  **Eradicate Silent Failures**: All verification layers must return a **status envelope** rather than a bare list of findings. Unverified or crashed executions must fail the gate.

---

## 3. Language Detection Rules

The verification runner maps a target `path` (file or directory) to a specific language/toolchain. Detection and image capability are decoupled.

Detection priority (primary matches take precedence, polyglots aggregate all matches):

1.  **Explicit `language=` Parameter**: Bypasses auto-detection (manual override).
2.  **File Paths (Extension Map)**:
    *   `.py` → Python
    *   `.js`, `.jsx`, `.mjs`, `.cjs` → JS
    *   `.ts`, `.tsx` → TS (scans upward for `tsconfig.json` to confirm project setup)
    *   `.go` → Go
3.  **Directory Paths (Project Marker Scan)**:
    *   `go.mod` → Go
    *   `package.json` → JS (detects Jest vs Vitest; maps to TS if `tsconfig.json` is present)
    *   `pyproject.toml` / `setup.py` / `requirements*.txt` / `Pipfile` / `tox.ini` → Python
    *   *Excludes directories like `node_modules`, `.venv`, `vendor`, `dist`, and `build`.*
4.  **Polyglot/Multiple Markers**: Returns an aggregated set of detected languages. Executes toolchains scoped to the respective subtrees containing the markers.
5.  **No Match (Unknown)**: Skips validation and prompts the user to explicitly specify `language=`. No silent fallback to Python.

---

## 4. Verification Status Model

Every validation layer (lint, type check, test) must return a structured status envelope:

```json
{
  "tool": "ruff",
  "status": "ok" | "findings" | "not_available" | "error" | "skipped",
  "findings": [ { "file", "line", "rule", "severity", "message" }, ... ],
  "detail": "...",      // Reason for error or skip
  "exit_code": 0
}
```

| Status | Meaning |
|---|---|
| `ok` | Executed successfully, exit code 0, no findings. |
| `findings` | Executed successfully, findings reported (gated based on severity). |
| `not_available`| Tool not present in image (exit code 127). Displayed separately; fails strict gates. |
| `error` | Tool crashed or exit code was unexpected, stderr reported, or output failed to parse. |
| `skipped` | Intentionally bypassed (e.g. Go project has no type check tool, or 0 tests found). |

### Connection to Gates

*   **Strict Gate (`publish` flow)**: If any required tool returns `not_available` or `error`, the gate fails (`gate_passed = false`) with the reason `"verification incomplete: <tool> <status>"`. **Unverified code cannot be published.**
*   **Lenient Gate (Interactive verify)**: Warnings are returned with `incomplete: true` to ensure the status remains visible.

---

## 5. Cleaning Up Silent Failures

1.  Stop suppressing stdout/stderr (`|| true` and `2>/dev/null` are removed).
2.  Unify the duplicate runner implementations (`lint_file` dispatching and `_run_*_verify` pipelines are combined into a single unified dispatching runner).
3.  Language detection is inserted upstream of `run_verify`.
4.  Remove silent Python fallbacks for unknown files.

---

## 6. Docker Image Tag & Layer Structure

### Layer Hierarchy (`FROM` chain)

*   **`sandbox:base`**: Contains language-agnostic utilities (`ripgrep`, `ast-grep`, `fd`, `sd`, `ctags`, `git`, `gh`, `jq`, `uv`) + **Python runtime + Node runtime**.
*   **Backend Layers** (inherit from `sandbox:base` using `FROM`):
    *   `sandbox:python`: Ruff, Pyright, Pytest + pytest-json-report.
    *   `sandbox:go`: Go compiler and build toolchains.

### Image Tags (Pinned to SHA-256 Digests)

| Tag | Layer Composition | Use Case |
|---|---|---|
| `sandbox:base` | base | Parent image and fallback for unknown/polyglot repositories. |
| `sandbox:python` | base + python + js | Selected for Python repositories. |
| `sandbox:go` | base + go (+ js) | Selected for Go repositories. |
| `sandbox:minimal` | Core Git + Python | Lightweight environment for rapid tests. |

---

## 7. Execution Coordination (Loud-Failure Contract)

If language detection returns `go` but the container is running `sandbox:python` (which lacks the Go toolchain), executing Go verification returns `not_available`. The strict validation gate fails with a clear message: `"detected go / this image has no go toolchain; re-initialize with sandbox:go"`.

Auto-detection occurs **on the host side prior to container startup** (via Shiori clones or GitHub API scans) so the correct image is selected at initialization (`sandbox_initialize`). If a mismatch occurs (e.g., custom override is set incorrectly), the loud-failure contract prevents publishing.

---

## 8. Migration & File Alignment

*   **Dockerfile Splitting**: Split `docker/Dockerfile.sandbox` (the legacy monolithic Python-only image) into `Dockerfile.base` and `Dockerfile.python`. 
    *   Ruff, Pyright, and pytest are moved to the Python backend layer.
    *   Standard CLI utilities, Python, and the **Node runtime** are placed in the base layer.
    *   Legacy monolithic files and workflows are removed (#313).
*   `docker/Dockerfile.sandbox.minimal` remains unchanged and is preserved as `sandbox:minimal`.
*   **CI Compilation Pipeline**: Base image builds ➔ determines base digest pin ➔ child variant images are compiled targeting the specific base digest ➔ all variants are published using `@sha256` digests. The `build-sandbox-variants.yml` workflow automatically creates PRs to update the `_NEUTRAL_IMAGE`, `_PYTHON_IMAGE`, and `_GO_IMAGE` digest constants inside `src/sunaba/tools/container.py` (#313).

---

## 9. Implementation Sequence

1.  **Image Layer Refactor & Node Integration**: Implement `Dockerfile.base`, `Dockerfile.python`, `Dockerfile.go`, and configure the CI workflows to build them.
2.  **Edit/Verify Subsystem Refactoring**: Implement the structured status envelope (§4) and language dispatch logic (§3), unifying duplicate execution runner branches.
3.  **Dedicated Detection Module**: Implement language detection rules (§3) inside a standalone helper module.
4.  **Language-Specific Semgrep Rules & Health Checks**: Configure Semgrep configuration flags by language and update health checks (§5.6).

---

## 10. Non-Goals (Out of Scope)

*   **Additional Languages (Java, Ruby, Rust, etc.)**: Suspended for v1.0. Support is restricted to Python, JS/TS, and Go. Additional compilers can be introduced in subsequent backend layers.
*   **On-Demand Runtime Installation**: Installing tools at runtime (e.g. `apt-get` or `npm install`) is rejected due to default-off network postures (§2), verification timeouts, and execution reproducibility constraints. Support for new runtimes must be handled using variant Docker images.
*   **Persistent Snapshots & Custom Networking**: Deferred in alignment with core design policies.
