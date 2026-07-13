# Sandbox Image & Language Detection

Sunaba runs AI edits, lints, and test checks inside purpose-built Docker sandbox images. This document describes the images, pre-installed toolchains, detection rules, and version compatibility contracts.

---

## 1. Sandbox Image Variants

To keep image sizes manageable, the single monolith sandbox image is split into modular variants built from `docker/Dockerfile.{base,python,go}`:

| Tag | Base Layer | Included Backend Toolchains | Use Case |
|---|---|---|---|
| `sandbox:base` | Neutral core | Node runtime + VCS + Search tools. No backend compilers/interpreters. | Fallback when project language is unknown or polyglot. |
| `sandbox:python` | Base | Python (3.12) toolchain + Node runtime (for Pyright). | Python projects. |
| `sandbox:go` | Base | Go compiler/toolchain. | Go projects. |
| `sandbox:minimal` | Minimal | Bare Git + Python + Pytest. | Lightweight or rapid testing. |

---

## 2. Tool Inventory

The default images come pre-installed with the following utilities, which the server uses to execute first-class verbs:

| Category | Utility | Used For |
|---|---|---|
| Text Search | `ripgrep` (`rg`) | Lexical search in `search_in_container` |
| Structural Search | `ast-grep` (`sg`) | Structural AST search in `search_in_container` |
| Text Replace | `sd` | CLI-based search-and-replace edits |
| File Search | `fd` | Finding file paths |
| Code Indexing | `universal-ctags` | AST symbol navigation |
| Linting | `ruff` / `eslint` | Python and JavaScript linting and autofix gates |
| Type Checking | `pyright` / `tsc` | Python and TypeScript static type verification |
| Version Control | `git`, `gh` | Cloning repositories, pushing changes, and managing issues |
| Package Install | `uv` / `pip` | Fast dependency resolution in `package_install` |
| JSON Processing | `jq` | Parsing and formatting command outputs |

---

## 3. Language Detection & Selection Rules

When `sandbox_initialize` is called without specifying an explicit `image` parameter, the server automatically detects the project's language by querying files from GitHub and chooses the appropriate image:

1.  **Manual Override**: Passing `language=` to `verify_in_container` or an explicit `image=` to `sandbox_initialize` overrides auto-detection.
2.  **File Extension (Single-file fallback)**:
    *   `.py` → `sandbox:python`
    *   `.go` → `sandbox:go`
    *   `.js`, `.jsx`, `.ts`, `.tsx` → `sandbox:base`
3.  **Project Marker Files (Repository scanning)**:
    *   `go.mod` → Go project (`sandbox:go`)
    *   `pyproject.toml` / `setup.py` / `requirements*.txt` / `Pipfile` → Python project (`sandbox:python`)
    *   `package.json` → Node project (`sandbox:base`)
4.  **Fallback**: Unknown, polyglot (e.g. Python + Go mixed without override), or unsupported languages default to the neutral `sandbox:base`.

---

## 4. Image Compatibility & Semver Policy

The external contract of the Sunaba server is versioned using [Semantic Versioning](https://semver.org/).

*   **Covered by Semver**: MCP tool names, argument names/types, return shapes, and environment variables. Breaking modifications (e.g. removing a tool or argument) require a minor version bump while in `0.x` (or major bump in `1.x+`).
*   **NOT Covered by Semver**: The sandbox image contents, internal module layout, and digests. 
*   **Image Pins**: Image digests are pinned inside `image_pins.json` (updated via `scripts/update_image_pins.py`). Any server release is guaranteed to work with the digests bundled in its release package. If you supply a custom image via `image=`, ensure the expected tools (e.g., `ruff`, `pyright`, `pytest`, `eslint`, `jest`, `go test`) are installed and behave compatibly with the server's edit/verify wrappers.
