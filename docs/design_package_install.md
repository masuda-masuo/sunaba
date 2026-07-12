# Package Install Tool — Design Document

> Issue: #262
> Position: Replaces running `pip install` via `sandbox_exec` with a dedicated tool.
> `sandbox_exec` is meant to be restricted to VCS/git operations, while package installations are centralized in this tool.

---

## 1. Background

Historically, `sandbox_exec pip install --quiet -e .[dev] 2>&1 | tail -3` was used to install packages. However, the LLM does not need to read the verbose dependency resolution logs, which unnecessarily pollutes the context window.

---

## 2. Requirements

*   **Input**: `container_id` (required), `packages` (`str | list[str]`), `constraints`, `requirements`, `editable`, `extras`, `upgrade`, etc.
*   **Output**: Success/Failure status + `installed_packages` (a summary list of newly installed packages) + error details.
*   Runs `pip install` internally but returns a clean, structured output.
*   The first step toward separating pip operations from generic shell commands (`sandbox_exec`).

---

## 3. Output Formats

### On Success
```json
{
  "status": "ok",
  "installed_packages": ["package1==1.0.0", "package2==2.1.0"],
  "changed": 2,
  "output": "Successfully installed package1-1.0.0 package2-2.1.0"
}
```

### On Failure
```json
{
  "status": "error",
  "error": "pip install failed (exit code 1)",
  "stderr": "ERROR: Could not find a version that satisfies the requirement nonexistent-package"
}
```

---

## 4. Implementation Details

*   Implemented as the `package_install` function in a new tool file: `src/sunaba/tools/package.py`.
*   Registered via `mcp.tool()` in `server.py`.
*   Executes `pip install` programmatically using `exec_run` (similar to subprocess).
*   Retrieves the list of installed packages before and after execution using `pip list --format=json`, computing the diff to populate `installed_packages`.
*   ~~**Discarded (Issue #380)**: Prefer `uv pip install` if `uv` is present.~~ The standard sandbox image does not run as root and lacks an active venv by default, which causes `uv pip install` to fail (requires `--system` or `--user` flags, neither of which work cleanly with uv). Raw `pip` falls back to user-site automatically and was chosen instead (Issue #383 / PR #384). 
*   **Re-enabled (Issue #390)**: PR #388 introduced a persistent venv (`$VIRTUAL_ENV`) inside the default image. The execution logic was updated to dynamically check: *"If `$VIRTUAL_ENV` is set and `uv` is available, use `uv pip install`; otherwise fall back to raw `pip install`."* For custom images without virtual environments, the fallback to user-site pip install continues to work cleanly.

---

## 5. Security & Performance

*   **Command Injection Prevention**: Arguments are passed as an explicit list directly to `exec_run` (bypassing the shell). This prevents shell command injections even if package names contain special characters.
*   **Containment**: Runs entirely inside the container; no host impact.
*   **Performance**: Running `pip list --format=json` twice adds a few seconds of overhead in environments with a large number of packages (>1000). For sandboxing purposes, this latency is negligible.

---

## 6. Comparison with Legacy Commands

| Operation | Legacy Command | Recommended (Using this Tool) |
|---|---|---|
| Install package | `sandbox_exec pip install requests` | `package_install(container_id, packages="requests")` |
| Editable install | `sandbox_exec pip install -e .[dev]` | `package_install(container_id, editable=".", extras="[dev]")` |
| Requirements file | `sandbox_exec pip install -r req.txt` | `package_install(container_id, requirements="req.txt")` |

---

## 7. Out of Scope (What We Won't Do)

*   **Non-python Package Managers**: Package managers like `npm`, `cargo`, or `go get` are not supported by this tool (run them via `sandbox_exec` instead).
*   **Virtual Environment Management**: Automated creation/deletion of virtual environments is out of scope (the container *is* the virtual environment).
*   **Dependency Auditing**: Visualizing dependency trees or audits is not supported.
