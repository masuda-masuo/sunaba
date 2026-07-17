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

1.  **Node Runtime in the Base Layer; Dev Tools in Backend Layers**: Node itself is a cross-cutting *runtime* (needed for Pyright as well as frontend code) and is bundled into the base image. JS *development* tools (`eslint` / `tsc` / `jest`) are not part of that cross-cutting concern -- they are project-specific dev/testing packages, so (mirroring Python and Go) they live in a dedicated backend layer (`docker/Dockerfile.js`, sharing `install-js-tools.sh` with `Dockerfile.full`; Issue #588) rather than in `base`.
2.  **Unified Dispatch Matrix / Modular Images**: Language detection rules will support Python, JS, TS, and Go. Missing tools will be treated as first-class `not_available` statuses rather than silent skips.
3.  **Monolith Prevention (revised by #584)**: The *layer split* stands — runtimes required by cross-cutting infra belong in the `base` image, language-specific development/testing packages belong in their respective backend layers. What was wrong was applying that principle to the **runtime default**. See §6.1.
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
    *   `package.json` → JS (maps to TS if `tsconfig.json` is present)
    *   `pyproject.toml` / `setup.py` / `requirements*.txt` / `Pipfile` / `tox.ini` → Python
    *   *Excludes directories like `node_modules`, `.venv`, `vendor`, `dist`, and `build`.*

    Jest vs Vitest is a *test-runner* distinction, not a language, so it is
    resolved one level down: the `test` layer's jest runner reads
    `package.json` itself before invoking anything (`_detect_js_test_runner`,
    Issue #588). A vitest-only project (`vitest` present, `jest` absent)
    reports a clear `skipped` status instead of being forced through the
    jest CLI, which would misparse vitest's own output. No `VitestAdapter`
    exists yet; the skip message says so explicitly.
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

*   **`sandbox:base`**: Contains language-agnostic utilities (`ripgrep`, `ast-grep`, `fd`, `sd`, `ctags`, `git`, `gh`, `jq`, `uv`) + **Python runtime + Node runtime** (+ a non-root npm global prefix, `NPM_CONFIG_PREFIX`, so backend layers can `npm install -g` without root).
*   **Backend Layers** (inherit from `sandbox:base` using `FROM`):
    *   `sandbox:python`: Ruff, Pyright, Pytest + pytest-json-report.
    *   `sandbox:go`: Go compiler and build toolchains.
    *   `sandbox:js`: eslint, typescript (tsc), jest (Issue #588).

### Image Tags (Pinned to SHA-256 Digests)

| Tag | Layer Composition | Use Case |
|---|---|---|
| `sandbox:full` | base + python + go + js | **The default.** Started whenever `image=` is omitted. |
| `sandbox:base` | base | `FROM` parent of the variants. Not a runtime default (#584). |
| `sandbox:python` | base + python | Lean image, reachable only via an explicit `image=`. |
| `sandbox:go` | base + go | Lean image, reachable only via an explicit `image=`. |
| `sandbox:js` | base + js | Lean image, reachable only via an explicit `image=` (#588). |
| `sandbox:minimal` | Core Git + Python | Lightweight environment for rapid tests. |

The toolchain installs live in `docker/install-python-tools.sh` / `install-go.sh` / `install-js-tools.sh`, which `Dockerfile.python` / `Dockerfile.go` / `Dockerfile.js` / `Dockerfile.full` all source. Two copies of an install step drift, and that drift *was* #584: `pytest-json-report` was baked into the python image only, so every container started from any other image failed its first verify.

**js dev tools are baked, not pip-install-like.** Unlike Python's `pip install -e .[dev]`, which writes into the same venv the image already put on `PATH` so the repo naturally wins, `npm install -g` and a repo's own `node_modules` are two entirely separate trees -- nothing makes the repo win by default. A globally baked eslint 9 hitting a repo pinned to eslint 8's config is a silent version mismatch, not an error. So `edit_verify`'s eslint/tsc/jest runners resolve `node_modules/.bin/<tool>` relative to the verify working directory *first*, falling back to the image-baked global only when the repo has no local install, and always record which one ran in the `VerifyResult.detail` field (`_resolve_js_tool` / `_annotate_resolution`, Issue #588). This is a resolution-order fix, not an image-splitting one: shipping a separate `sandbox:js` image does not by itself solve the version-mismatch problem -- the per-invocation resolution does.

### 6.1 Why the default is the union, not a guess (#584)

Language detection ran **twice**, and the two runs were not equals:

| | Host-side image selection (removed) | `edit_verify.detect_languages` (kept) |
|---|---|---|
| When | Before the container starts | Inside the container, on every verify |
| Evidence | A GitHub contents-API probe over the network | The project's real files |
| Reversible | **No** — an image is immutable once running | Yes — the next call re-runs it |

The accurate detector ran *after* the irreversible decision. When the probe failed (rate limit, timeout, private repo without a token), init silently landed on an image that lacked the toolchain the code actually needed, and the first verify failed the gate for a reason that had nothing to do with the code. There was no way to fix it after the fact.

The fix is to remove the guess, not to make it more reliable: **the default image is a superset of the dispatch matrix.** Whatever the in-container detector concludes, the tools are there. Host-side detection (`image_selection.py`) is deleted; `image=` remains the escape hatch and the only way to ask for a lean variant.

This costs almost nothing. The server prewarms images anyway, and it used to prewarm base + python + go (≈1.34 GB resident on the host) precisely so detection would never hit a cold pull. The all-in-one image is the same ≈1.34 GB, and it is now the *only* one prewarmed. Unused binaries cost nothing at runtime; layers are shared copy-on-write across containers.

Consequences:

*   **The image contract is "⊇ dispatch matrix."** Each image's `HEALTHCHECK` asserts the tools it owes verify; CI runs it with `docker run`, so a missing tool fails the build rather than a user's first verify.
*   **`not_available` regains its meaning**: "sunaba has no toolchain for this language at all" (e.g. Rust) — an honest signal — rather than "the GitHub probe lost a coin flip."
*   **py+go polyglot works for the first time.** It used to fall back to the neutral base, which has *neither* toolchain, so the gate failed no matter what.

---

## 7. Execution Coordination (Loud-Failure Contract)

If language detection returns `go` but the container is running `sandbox:python` (which lacks the Go toolchain), executing Go verification returns `not_available`. The strict validation gate fails with a clear message: `"detected go / this image has no go toolchain; re-initialize with sandbox:go"`.

There is **no host-side auto-detection** (#584, §6.1). Language dispatch happens only inside the container, against the real files. A mismatch is therefore possible only when the caller passed an explicit `image=` that lacks the toolchain the project needs — a deliberate act — and the loud-failure contract prevents publishing unverified code.

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
*   **On-Demand Runtime Installation**: Installing tools at runtime (e.g. `apt-get` or `npm install`) is rejected due to default-off network postures (§2), verification timeouts, and execution reproducibility constraints. Support for new runtimes must be handled by baking them into the images. This is why #584 was fixed by widening the default image rather than by having `verify` `pip install` its own missing plugin: a container is network-off by default, and the next missing tool would reopen the same hole.
*   **Persistent Snapshots & Custom Networking**: Deferred in alignment with core design policies.
