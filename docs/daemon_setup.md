# Resident Daemon Setup (systemd)

For optimal performance and security, Sunaba is designed to run as a resident background service (daemon) under `systemd`. 

Standard stdio-based MCP setups are subject to client-side 60-second timeouts, which can abort long-running Docker pulls or test runs. Running Sunaba as a systemd service using the `streamable-http` transport completely avoids stdio timeouts, handles automatic credential rotation, and allows sharing the sandbox container across multiple IDE clients (e.g., opencode and Claude Desktop).

```
Claude Desktop ── mcp-remote ┐
                             ├─ sunaba (WSL2 / Linux, streamable-http @ 127.0.0.1:8750/mcp)
opencode ────────────────────┘
```

---

## 1. The Shared Security Model: Short-Lived Tokens

Sunaba uses [mcp-launcher](https://github.com/masuda-masuo/mcp-launcher)'s `mcp-token` broker:

*   **Secrets live in the OS keystore, never in config files.** The GitHub App credentials (`APP_ID`, `PRIVATE_KEY`, `INSTALLATION_ID`) are registered once.
*   **Tokens are short-lived and minted on demand.** `mcp-token` mints a fresh installation token (cached for ~55 minutes; expires in 1 hour).
*   **Host-side resolution**: The resolved token is used host-side by the egress proxy and write tools. The container itself never receives the token.

The host resolves the token from one of three sources (checked in priority order: broker mint → App Installation Token → static `GITHUB_TOKEN`):

| Source | Host Env Var | How it works |
|---|---|---|
| Vendored broker | `GITHUB_TOKEN_BROKER_SERVICE` | Resolves and runs the `mcp-token` binary (checksum-verified, cached). Recommended. |
| Explicit command | `GITHUB_TOKEN_COMMAND` | Runs a custom command (e.g. `mcp-token github`); stdout becomes the token. |
| Static token | `GITHUB_TOKEN` | Uses the token verbatim. Minimal setup fallback. |

---

## 2. Setup Guide (WSL2 & Linux)

Setting up the resident systemd user service is done in three phases.

### Phase 1: Install
Create a Python virtual environment and install the package:

```bash
python -m venv ~/.local/share/sunaba-venv
~/.local/share/sunaba-venv/bin/pip install \
    git+https://github.com/masuda-masuo/sunaba@v0.9.0
```

### Phase 2: Setup (Interactive Keyring Registration)
Register your GitHub App credentials into the OS keystore:

```bash
./scripts/setup.sh
```

This script automatically resolves `mcp-token` (downloading it anonymously if not cached), prompts for your GitHub App details, and registers them in the OS keystore.

### Phase 3: Enable the systemd Service
Place the systemd user unit and start the service:

```bash
./scripts/install-systemd.sh ~/.local/share/sunaba-venv
```

The script substitutes path variables, writes the service unit file to `~/.config/systemd/user/sunaba.service`, reloads systemd, and starts the unit.

---

## 3. Configuration & Integration

### Systemd User Unit (`sunaba.service` excerpt)
The template runs the server on streamable-HTTP at localhost, pointing to the credential broker and forwarding DBus coordinates to reach the keyring:

```ini
[Service]
ExecStart=@VENV_DIR@/bin/python -m sunaba.server \
    --transport streamable-http --host 127.0.0.1 --port 8750
Environment=GITHUB_TOKEN_BROKER_SERVICE=sunaba
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/%U/bus
```

### Connecting from Clients (e.g. Claude Desktop)
Clients connect to the background daemon using `mcp-remote` over HTTP:

```json
{
  "mcpServers": {
    "sunaba": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://127.0.0.1:8750/mcp"]
    }
  }
}
```

---

## 4. Keyring Requirements & Gotchas

*   **Keystore Backend**: Sunaba uses **GNOME Keyring (libsecret)** under Linux and WSL2.
*   **Unlock on Boot**: On WSL2, you will be prompted for your password once on the first access after boot. Native Linux desktop sessions unlock the keyring automatically at login.
*   **DBus Session**: The systemd service must have access to `DBUS_SESSION_BUS_ADDRESS`. Without it, the service cannot reach GNOME Keyring to fetch credentials.
*   **WSL2 keyrings**: `mcp-token list` may fail in some WSL2 environments because Go's `godbus` library cannot reach the DBus secret service interface directly. If this occurs, query keys using the native CLI instead:
    ```bash
    secret-tool search --all service mcp-launcher
    ```
