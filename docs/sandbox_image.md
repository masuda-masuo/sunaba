# Sandbox Image & Language Detection

Sunaba runs AI edits, lints, and test checks inside purpose-built Docker sandbox images. This document describes the images, pre-installed toolchains, detection rules, and version compatibility contracts.

---

## 1. Sandbox Image Variants

Images are built from `docker/Dockerfile.{base,python,go,full}`. The toolchain installs themselves live in `docker/install-python-tools.sh` and `docker/install-go.sh`, which the variant Dockerfiles all source, so "what the Python toolchain is" is defined in exactly one place.

| Tag | Base Layer | Included Backend Toolchains | Use Case |
|---|---|---|---|
| `sandbox:full` | Base | Python (3.12) + Go + Node. Every toolchain `verify` can dispatch to. | **The default.** Used whenever `image=` is omitted. |
| `sandbox:base` | Neutral core | Node runtime + VCS + Search tools. No backend compilers/interpreters. | `FROM` parent of the variants. Not a runtime default. |
| `sandbox:python` | Base | Python (3.12) toolchain + Node runtime (for Pyright). | Lean image; explicit `image=python` only. |
| `sandbox:go` | Base | Go compiler/toolchain. | Lean image; explicit `image=go` only. |
| `sandbox:minimal` | Minimal | Bare Git + Python + Pytest. | Lightweight or rapid testing. |

The default is deliberately the **union** of the toolchains rather than a guess at which one this project needs. See `design_multilang_support.md` §6.1 (Issue #584) for why: the host cannot reliably know a repository's language before the container starts, and the image is immutable once it does, so a wrong guess is unfixable — while the in-container detector that runs later is always right.

Each image's `HEALTHCHECK` asserts the tools it owes `verify`, and CI runs it with `docker run` after every build. A tool missing from an image therefore fails the build, not a user's first `verify`.

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

**Image selection does not involve detection.** `sandbox_initialize` starts `sandbox:full` unless an explicit `image=` says otherwise (the aliases `full` / `neutral` / `python` / `go` resolve to pinned digests). The host used to probe the GitHub contents API to pick a variant; that was removed in #584 because the guess preceded an irreversible decision and a failed probe silently produced a container missing the toolchain the project needed.

**Language detection still happens — inside the container, at verify time**, where it reads the real files and can be re-run. It selects which toolchain to *run*, not which image to start (`edit_verify.detect_languages`):

1.  **Manual Override**: `language=` on `verify_in_container` / `lint_in_container` / `type_check_in_container` skips detection.
2.  **File Extension** (single-file targets): `.py` → Python, `.go` → Go, `.js` / `.jsx` → JS, `.ts` / `.tsx` → TS (scans upward for `tsconfig.json`).
3.  **Project Marker Files** (directory targets): `go.mod` → Go; `pyproject.toml` / `setup.py` / `requirements*.txt` / `Pipfile` / `tox.ini` → Python; `package.json` → JS; `tsconfig.json` → TS.
4.  **Polyglot**: all detected languages run, each scoped to the sub-tree holding its marker. The default image carries every toolchain, so a polyglot repository is verified in full rather than falling back to an image that can run neither.
5.  **Unknown**: no markers found → verify asks for an explicit `language=` instead of silently guessing.

---

## 4. Image Compatibility & Semver Policy

The external contract of the Sunaba server is versioned using [Semantic Versioning](https://semver.org/).

*   **Covered by Semver**: MCP tool names, argument names/types, return shapes, and environment variables. Breaking modifications (e.g. removing a tool or argument) require a minor version bump while in `0.x` (or major bump in `1.x+`).
*   **NOT Covered by Semver**: The sandbox image contents, internal module layout, and digests. 
*   **Image Pins**: Image digests are pinned inside `image_pins.json` (updated via `scripts/update_image_pins.py`). Any server release is guaranteed to work with the digests bundled in its release package. If you supply a custom image via `image=`, ensure the expected tools (e.g., `ruff`, `pyright`, `pytest`, `eslint`, `jest`, `go test`) are installed and behave compatibly with the server's edit/verify wrappers.
