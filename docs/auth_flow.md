# VCS Authentication & Token Lifecycle

This document describes how Sunaba resolves, rotates, and isolates VCS (GitHub) credentials to maintain high security while executing Git operations inside disposable sandboxes.

---

## 1. Credential Containment (No In-Container Tokens)

A core security principle of Sunaba is that **VCS tokens never enter the container filesystem**. 

*   The container's local `git` and `gh` config environments are completely unauthenticated.
*   If an attacker executes malicious code inside the sandbox and attempts to inspect `.git/config`, `~/.git-credentials`, `~/.config/gh/hosts.yml`, or system environment variables, they will find **no credentials**.
*   This prevents credentials from leaking if container-local command logs or file state are exposed.

---

## 2. Host-Side Token Resolution

All authentication resolving happens on the host machine. The server queries three independent providers sequentially until a token is successfully resolved:

```
               [Request Token]
                      │
                      ▼
        +───────────────────────────+
        │ 1. Token Broker (Keystore)│  --> Success? --> Return Token
        +───────────────────────────+
                      │ Fail
                      ▼
        +───────────────────────────+
        │ 2. GitHub App Provider    │  --> Success? --> Return Token
        +───────────────────────────+
                      │ Fail
                      ▼
        +───────────────────────────+
        │ 3. Static Environment PAT │  --> Success? --> Return Token
        +───────────────────────────+
                      │ Fail
                      ▼
               [Anonymous / Fail]
```

### Resolution Hierarchy

1.  **Token Broker (`token_broker.py`)**:
    Queries the host-side keystore broker command if `GITHUB_TOKEN_COMMAND` or `GITHUB_TOKEN_BROKER_SERVICE` is set. This generates extremely short-lived tokens and represents the highest security posture.
2.  **GitHub App Provider (`github_auth.py` / `AppTokenProvider`)**:
    If a private key is placed on the host, the provider issues Installation Access Tokens (IAT) and caches them in memory. A daemon thread handles automatic refreshes prior to expiration.
3.  **Static Environment PAT (`GITHUB_TOKEN` / `GH_TOKEN`)**:
    Reads the static personal access token from the host process environment. Used primarily for local bootstrapping and testing.

---

## 3. Delegated Authentication via Egress Proxy

For operations requiring network access (like `sandbox_initialize(clone_repo=...)`), the container needs to authenticate with GitHub. Because the token cannot be placed in the container, Sunaba delegates authentication via the host-side Egress Proxy.

```
+───────────────────────────────+            +─────────────────────────+
| DISPOSABLE SANDBOX            |            | HOST SERVER             |
|                               |            |                         |
|  $ git clone (No Token)       |            |   Resolves Host Token   |
|         │                     |            |            │            |
|         ▼ (Proxy Request)     |            |            ▼            |
|   egress-proxy sidecar  ──────┼───────────►│   Authorizes window     |
|                               |  Authorize |   Injects Token to Proxy|
|   Intercepts & rewrites req   |◄───────────┼────────────┘            |
|   Injects Auth headers        |            |                         |
+─────────┬─────────────────────+            +─────────────────────────+
          │
          ▼ (Authenticated Request)
      GitHub API / Git Remote
```

### The Authorization Window Sequence
1.  **Pre-authorization**: Before executing a tool that crosses the network boundary (e.g. `sandbox_initialize(clone_repo=...)`), the host server resolves the target VCS token.
2.  **Grant Registry**: The host grants a temporary "authorization window" to the egress proxy for that specific repository and session.
3.  **Credential Injection**: When the container's standard Git client requests resources through the proxy sidecar, the proxy intercepts the traffic, matches the target repository, and injects the resolved authorization header dynamically.
4.  **Window Closure**: Once the operation completes, the authorization window closes, and subsequent container requests to that repository are blocked.

---

## 4. Log Masking & De-noising

To prevent resolved tokens from leaking into local observability endpoints (like journals or HTML traces), Sunaba implements aggressive log cleaning:

*   **Credential Sanitizer**: All tool outputs, stdout/stderr streams, and execution arguments pass through `sanitize_output()` before being logged.
*   **Masking Rules**: Recognized API keys, token headers, and strings matching `gho_*`, `ghp_*`, `github_pat_*`, or credentials defined in proxy environment strings are automatically replaced with `***` or `KEY=***`.
