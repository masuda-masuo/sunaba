# Filesystem Layout & Safety Design

This document details the filesystem layout, directory boundaries, and security-first transfer mechanisms designed to prevent accidental commits of temporary files and ensure host filesystem protection.

---

## 1. Directory Layout Overview

Sunaba divides state and execution into three isolated filesystem zones:

```
+-----------------------------------------------------------------+
| HOST MACHINE                                                    |
|                                                                 |
|  [Operator Workspace]                                           |
|   └── /path/to/project/  <-- Clean local source code            |
|                                                                 |
|  [Sunaba Home State]                                            |
|   └── ~/.sunaba/                                                |
|        ├── journal.log   <-- Telemetry outside VCS              |
|        └── traces/       <-- HTML replays outside VCS           |
+-----------------------------------------------------------------+
                                │
               Host-to-Container Ingress (Tar Stream)
                                │
                                ▼
+-----------------------------------------------------------------+
| DISPOSABLE DOCKER CONTAINER                                     |
|                                                                 |
|  [Isolated Work Space]  <-- also the container's WORKDIR         |
|   └── /workspace/        <-- Cloned git repo under test         |
|                                                                 |
|  [Container User's Home]                                        |
|   └── /home/sandbox/     <-- venv, caches, .sandbox-meta.json   |
|                                                                 |
|  [Ephemeral Scratch Space]                                      |
|   └── /tmp/              <-- Patches, transforms, AST runs      |
+-----------------------------------------------------------------+
```

---

## 2. Host-Side Isolation (`~/.sunaba/`)

All telemetry, execution journals, replay traces, and API credential caches are stored in a centralized directory on the host machine: `~/.sunaba/` (resolved platform-specifically via `platformdirs`).

### Accidental Commit Prevention
Because all journal logs, traceback dumps, and HTML trace files are stored in `~/.sunaba/` rather than the active workspace directory, **they never appear in git status**. 

The AI can run `git add .` or `git commit` inside the workspace without any risk of accidentally staging or pushing diagnostic logs, error files, or telemetry back to the remote repository.

---

## 3. Container-Side Isolation (`/workspace` vs `/tmp`)

Inside the disposable sandbox container, the filesystem is divided into distinct zones to prevent code compilation, testing, and modification from polluting the Git staging area.

### `/workspace` (Git Repository Root)
This is the workspace containing the cloned target repository. Only changes intended for Git commits reside here.

`/workspace` is also the container's **working directory** (`docker run --workdir`, mirrored by `WORKDIR` in the images). This is what makes the repository root unambiguous: a command that names no working directory still runs inside the repository, so a tool that forgets to pass one cannot silently operate outside it (Issue #600). It also means the host never has to ask the container where the repository is — the host chose the path at creation time, and Docker records it in the container's config.

### `/home/sandbox` (Container User's Home)
The sandbox user's home holds machine state, not project state: the pre-seeded virtualenv, tool caches, `issue.md` dumps, and `.sandbox-meta.json` (the clone metadata written at init). It is deliberately *outside* the workspace — anything written here is invisible to `git status`, exactly like the host-side `~/.sunaba/` above.

### `/tmp` (Scratch Space)
Temporary operations performed by tools are directed to the container's ephemeral `/tmp` directory instead of the `/workspace` folder. Examples include:
*   Decoded patch blocks before they are applied.
*   Intermediate files generated during Python or Node AST code transforms (`transform_file`).
*   Linting and type check output reports.

#### Accidental Commit Prevention
By confining all intermediate compilation files and diagnostic scratchpads to `/tmp` (which resides outside the git workspace root), the Git index remains pristine. Running `git status` inside `/workspace` only reports actual code modifications, completely eliminating the risk of staging temporary patch files or tool-generated logs.

---

## 4. Ingress File Transfer Design (One-Way Tar Pipe)

To copy local code from the host into the sandbox for testing and validation (e.g. via `copy_project`), Sunaba avoids mounting host directories (which would expose the host filesystem to risks).

### The In-Memory Tar Pipe Mechanism
1.  **Compression on Host**: The server reads files from the host path and compresses them into a tarball stream in-memory.
2.  **Streaming over Stdin**: The tarball byte-stream is pushed directly to the container's standard input via Docker's exec/stdin API.
3.  **Unpacking in Sandbox**: The container receives the stream and extracts the files into `/workspace` using the container's local `tar` binary.

### Safety Guarantees
*   **No Host Mounts**: The container has no network or filesystem handles back to the host machine.
*   **Path Traversal Prevention**: Because the extraction is executed entirely within the container using standard container-local processes, path traversal attacks (e.g. attempting to overwrite host-side `/etc/shadow` by specifying `../../` paths) are contained within the sandbox's boundaries. The host filesystem remains completely write-protected.
