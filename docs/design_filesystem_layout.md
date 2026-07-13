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
|  [Git Repository Root]                                          |
|   └── /tmp/repo/{name}/  <-- Cloned git repo under test         |
|                              (default clone_dest)               |
|                                                                 |
|  [Ephemeral Scratch Space]                                      |
|   └── /tmp/              <-- Patches, transforms, AST runs      |
|                                                                 |
|  [User Home]                                                    |
|   └── /home/sandbox/     <-- Default CWD, sandbox-meta.json     |
+-----------------------------------------------------------------+
```

The container runs as a non-root user ``sandbox`` (home ``/home/sandbox/``).
``/tmp/`` is world-writable and is used as both the scratch space and the
default ``clone_dest`` parent directory.

---

## 2. Host-Side Isolation (`~/.sunaba/`)

All telemetry, execution journals, replay traces, and API credential caches are stored in a centralized directory on the host machine: `~/.sunaba/` (resolved platform-specifically via `platformdirs`).

### Accidental Commit Prevention
Because all journal logs, traceback dumps, and HTML trace files are stored in `~/.sunaba/` rather than the active workspace directory, **they never appear in git status**. 

The AI can run `git add .` or `git commit` inside the workspace without any risk of accidentally staging or pushing diagnostic logs, error files, or telemetry back to the remote repository.

---

## 3. Container-Side Isolation

Inside the disposable sandbox container, the filesystem is divided into distinct zones:

### ``/tmp/repo/{name}/`` (Git Repository Root)

The default clone destination (``clone_dest``) is ``/tmp/repo/``; the actual
repository lands at ``/tmp/repo/{owner-repo}/``.  ``/tmp/`` was chosen over a
dedicated ``/workspace/`` directory because:

1. **Non-root constraint**: The container runs as ``sandbox`` (UID 1000), not
   root. ``/tmp/`` is world-writable in the image, so no special ``chown`` or
   directory creation is needed at container start (Issue #91).
2. **Simplicity**: The default Docker image (``sandbox:full``) has no pre-created
   ``/workspace/`` directory. Using ``/tmp/repo/`` avoids an extra image layer
   or startup init step.

### ``/tmp/`` (Scratch Space)

Temporary operations performed by tools are directed to the container's
ephemeral ``/tmp/`` directory. Examples include:

*   Decoded patch blocks before they are applied.
*   Intermediate files generated during Python or Node AST code transforms
    (``transform_file``).
*   Linting and type check output reports.

#### Accidental Commit Prevention

By confining all intermediate compilation files and diagnostic scratchpads to
``/tmp/`` (which resides outside the git workspace root), the Git index
remains pristine.  Running ``git status`` inside ``/tmp/repo/{name}/`` only
reports actual code modifications.

### ``/home/sandbox/`` (Default CWD)

When a tool calls ``container.exec_run`` without an explicit ``workdir``, the
Docker SDK defaults to the container's working directory
(``/home/sandbox/``).  This directory is the `sandbox` user's home and is the
fallback when the git root cannot be auto-detected.

### workdir auto-detection

All dispatch runners use ``_exec_container()`` (defined in
``edit_verify.py``), which calls ``resolve_git_root()`` when ``workdir`` is
not provided.  This means new runners never need to know the physical repo
location; the auto-detection chain (``.sandbox-meta.json`` → ``/home/sandbox``
git test → ``/tmp/repo/*/`` scan → ``/home/sandbox`` fallback) finds it
transparently.

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
